"""Product 2 — Level-2 OrgPrincipal lifecycle (list / detail / enable / disable).

Owner may inspect descendants and manage L2 when the L1 parent is active.
Level-1 may inspect/manage only direct L2 children.
Level-2 cannot manage L2. Hierarchy fields are immutable.

Reuses Phase 2D error type, view privacy, and enable/disable semantics.
Does not provision children, bind Telegram, or change org_scope / authz.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrgPrincipal
from app.services.org_principals import (
    DEPTH_ONE,
    DEPTH_OWNER,
    DEPTH_TWO,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    get_principal,
    is_owner_principal,
)
from app.services.platform_identity import is_explicit_owner_staff
from app.services.volume_allocation_cascade import ParentVolumeError, assert_parent_has_room_for_reenabled
from app.services.principal_lifecycle import (
    PrincipalLifecycleError,
    _invalidate_pg_cache,
    _provision_role_meta,
    _safe_pg_role_name,
    _web_identity_status,
)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Level2PrincipalView:
    """Safe manager-facing L2 summary — never carries credentials."""

    principal_id: int
    status: str
    parent_id: int | None
    parent_username: str | None
    depth: int
    pg_username: str | None
    pg_role_id: int | None
    pg_role_name: str | None
    web_identity_status: str
    bot_binding_status: str  # bound | unbound
    created_at: datetime | None
    can_manage: bool

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "status": self.status,
            "parent_id": self.parent_id,
            "parent_username": self.parent_username,
            "depth": self.depth,
            "pg_username": self.pg_username,
            "pg_role_id": self.pg_role_id,
            "pg_role_name": self.pg_role_name,
            "web_identity_status": self.web_identity_status,
            "bot_binding_status": self.bot_binding_status,
            "created_at": (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
            "can_manage": bool(self.can_manage),
        }


def _positive_id(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _bot_binding_status(row: OrgPrincipal) -> str:
    return "bound" if _positive_id(getattr(row, "bot_user_id", None)) else "unbound"


async def load_management_actor(
    session: AsyncSession, staff: Mapping[str, Any] | None
) -> OrgPrincipal:
    """Server-side actor for representative management.

    Owner or active depth-1 Principal. Role strings are not authority.
    Sub-representatives (depth 2) are denied.
    """
    if not staff:
        raise PrincipalLifecycleError("احراز هویت نشده", code="unauthenticated")
    pid = _positive_id(staff.get("org_principal_id"))
    if pid <= 0:
        raise PrincipalLifecycleError(
            "Principal فعال نیست",
            code="inactive_or_missing_principal",
        )
    actor = await get_principal(session, pid)
    if actor is None:
        raise PrincipalLifecycleError(
            "Principal یافت نشد",
            code="not_found",
        )
    if str(actor.status) != STATUS_ACTIVE:
        raise PrincipalLifecycleError(
            "Principal فعال نیست",
            code="inactive_or_missing_principal",
        )
    if is_owner_principal(actor) and is_explicit_owner_staff(staff):
        return actor
    if int(actor.depth) == DEPTH_ONE and not is_owner_principal(actor):
        from app.services.representative_unification import (
            staff_can_manage_representatives,
        )

        if not staff_can_manage_representatives(staff):
            raise PrincipalLifecycleError(
                "قابلیت ساخت نماینده برای این حساب فعال نیست",
                code="pg_capability_denied",
            )
        return actor
    raise PrincipalLifecycleError(
        "دسترسی به مدیریت Principal مجاز نیست",
        code="forbidden",
    )


def actor_is_owner(actor: OrgPrincipal) -> bool:
    return is_owner_principal(actor)


async def _to_l2_view(
    session: AsyncSession,
    row: OrgPrincipal,
    parent: OrgPrincipal,
    *,
    fetch_role_name: bool = False,
) -> Level2PrincipalView:
    role_id, role_name = await _provision_role_meta(session, int(row.id))
    if fetch_role_name and role_id is not None and role_name is None:
        client = None
        try:
            from app.services.pasarguard import get_pg_for_principal

            client = await get_pg_for_principal(session, principal_id=int(parent.id))
        except Exception:
            client = None
        role_name = await _safe_pg_role_name(role_id, client=client)
    web_status = await _web_identity_status(session, int(row.id))
    parent_active = str(parent.status) == STATUS_ACTIVE
    parent_uname = (str(parent.pg_username).strip() if parent.pg_username else None) or None
    return Level2PrincipalView(
        principal_id=int(row.id),
        status=str(row.status),
        parent_id=int(parent.id),
        parent_username=parent_uname,
        depth=int(row.depth),
        pg_username=(str(row.pg_username).strip() if row.pg_username else None) or None,
        pg_role_id=role_id,
        pg_role_name=role_name,
        web_identity_status=web_status,
        bot_binding_status=_bot_binding_status(row),
        created_at=getattr(row, "created_at", None),
        can_manage=parent_active,
    )


async def _assert_managed_level2(
    session: AsyncSession,
    actor: OrgPrincipal,
    principal_id: int,
) -> tuple[OrgPrincipal, OrgPrincipal]:
    """Target must be depth-2 under an in-scope L1. Never Owner. Never L2→L2."""
    pid = _positive_id(principal_id)
    if pid <= 0:
        raise PrincipalLifecycleError(
            "شناسه Principal نامعتبر است",
            code="invalid_id",
        )
    if pid == int(actor.id):
        raise PrincipalLifecycleError(
            "Principal سطح ۲ نمی‌تواند خودش را مدیریت کند",
            code="cannot_manage_self",
        )
    row = await get_principal(session, pid)
    if row is None:
        raise PrincipalLifecycleError("Principal یافت نشد", code="not_found")
    if is_owner_principal(row) or int(row.depth) == DEPTH_OWNER or row.parent_id is None:
        raise PrincipalLifecycleError(
            "نمی‌توان مالک را مدیریت کرد",
            code="cannot_manage_owner",
        )
    if int(row.depth) != DEPTH_TWO:
        raise PrincipalLifecycleError(
            "فقط Principal سطح ۲ از این مسیر قابل مدیریت است",
            code="not_level2",
        )
    parent = await get_principal(session, int(row.parent_id))
    if parent is None or int(parent.depth) != DEPTH_ONE:
        raise PrincipalLifecycleError(
            "والد سطح ۱ یافت نشد",
            code="parent_missing",
        )
    if is_owner_principal(actor):
        if parent.parent_id is None or int(parent.parent_id) != int(actor.id):
            raise PrincipalLifecycleError(
                "این Principal در محدوده مالک نیست",
                code="out_of_scope",
            )
    elif int(actor.depth) == DEPTH_ONE:
        if int(parent.id) != int(actor.id) or int(row.parent_id) != int(actor.id):
            raise PrincipalLifecycleError(
                "این Principal در محدوده شما نیست",
                code="out_of_scope",
            )
    else:
        raise PrincipalLifecycleError(
            "Principal سطح ۲ نمی‌تواند سطح ۲ را مدیریت کند",
            code="forbidden",
        )
    return row, parent


def _assert_parent_active_for_mutation(parent: OrgPrincipal) -> None:
    if str(parent.status) != STATUS_ACTIVE:
        raise PrincipalLifecycleError(
            "والد غیرفعال است — مدیریت فرزند مجاز نیست",
            code="parent_disabled",
        )


async def list_level2_principals(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    *,
    parent_id: int | None = None,
) -> list[Level2PrincipalView]:
    """List L2 children. Client ``parent_id`` is a filter only after server checks."""
    actor = await load_management_actor(session, staff)
    wanted_parent = _positive_id(parent_id) if parent_id is not None else 0

    if is_owner_principal(actor):
        l1_result = await session.execute(
            select(OrgPrincipal).where(
                OrgPrincipal.parent_id == int(actor.id),
                OrgPrincipal.depth == DEPTH_ONE,
            )
        )
        l1_rows = list(l1_result.scalars().all())
        l1_by_id = {int(r.id): r for r in l1_rows}
        if wanted_parent:
            parent = l1_by_id.get(wanted_parent)
            if parent is None:
                raise PrincipalLifecycleError(
                    "این Principal در محدوده مالک نیست",
                    code="out_of_scope",
                )
            l1_by_id = {int(parent.id): parent}
        parent_ids = list(l1_by_id.keys())
        if not parent_ids:
            return []
        result = await session.execute(
            select(OrgPrincipal)
            .where(
                OrgPrincipal.depth == DEPTH_TWO,
                OrgPrincipal.parent_id.in_(parent_ids),
            )
            .order_by(OrgPrincipal.parent_id.asc(), OrgPrincipal.id.asc())
        )
        rows = list(result.scalars().all())
        views: list[Level2PrincipalView] = []
        for row in rows:
            parent = l1_by_id.get(int(row.parent_id or 0))
            if parent is None:
                continue
            views.append(await _to_l2_view(session, row, parent, fetch_role_name=False))
        return views

    # Level-1: only direct children. Mismatched client parent_id → tamper DENY.
    if wanted_parent and wanted_parent != int(actor.id):
        raise PrincipalLifecycleError(
            "این Principal در محدوده شما نیست",
            code="out_of_scope",
        )
    result = await session.execute(
        select(OrgPrincipal)
        .where(
            OrgPrincipal.parent_id == int(actor.id),
            OrgPrincipal.depth == DEPTH_TWO,
        )
        .order_by(OrgPrincipal.id.asc())
    )
    rows = list(result.scalars().all())
    return [await _to_l2_view(session, row, actor, fetch_role_name=False) for row in rows]


async def get_level2_principal_detail(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    principal_id: int,
) -> Level2PrincipalView:
    actor = await load_management_actor(session, staff)
    row, parent = await _assert_managed_level2(session, actor, principal_id)
    return await _to_l2_view(session, row, parent, fetch_role_name=True)


async def disable_level2_principal(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    principal_id: int,
) -> Level2PrincipalView:
    actor = await load_management_actor(session, staff)
    row, parent = await _assert_managed_level2(session, actor, principal_id)
    _assert_parent_active_for_mutation(parent)
    if is_owner_principal(row) or int(row.depth) == DEPTH_OWNER:
        raise PrincipalLifecycleError(
            "نمی‌توان مالک را غیرفعال کرد",
            code="cannot_disable_owner",
        )
    if int(row.depth) != DEPTH_TWO:
        raise PrincipalLifecycleError(
            "سلسله‌مراتب Principal نامعتبر است",
            code="hierarchy_invalid",
        )
    if str(row.status) == STATUS_DISABLED:
        return await _to_l2_view(session, row, parent, fetch_role_name=True)

    row.status = STATUS_DISABLED
    await session.flush()
    _invalidate_pg_cache(int(row.id))
    log.info(
        "Product 2: disabled Level-2 principal_id=%s by actor_id=%s",
        int(row.id),
        int(actor.id),
    )
    return await _to_l2_view(session, row, parent, fetch_role_name=True)


async def enable_level2_principal(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    principal_id: int,
) -> Level2PrincipalView:
    actor = await load_management_actor(session, staff)
    row, parent = await _assert_managed_level2(session, actor, principal_id)
    _assert_parent_active_for_mutation(parent)
    if int(row.depth) != DEPTH_TWO or row.parent_id is None:
        raise PrincipalLifecycleError(
            "سلسله‌مراتب Principal نامعتبر است",
            code="hierarchy_invalid",
        )
    if int(row.parent_id) != int(parent.id):
        raise PrincipalLifecycleError(
            "تغییر والد/عمق مجاز نیست",
            code="hierarchy_immutable",
        )
    if is_owner_principal(row):
        raise PrincipalLifecycleError(
            "نمی‌توان Principal را به مالک تبدیل کرد",
            code="cannot_become_owner",
        )
    if str(row.status) == STATUS_ACTIVE:
        return await _to_l2_view(session, row, parent, fetch_role_name=True)

    row.status = STATUS_ACTIVE
    await session.flush()
    try:
        await assert_parent_has_room_for_reenabled(session, row)
    except ParentVolumeError as exc:
        raise PrincipalLifecycleError(str(exc), code="parent_volume") from exc
    log.info(
        "Product 2: re-enabled Level-2 principal_id=%s by actor_id=%s",
        int(row.id),
        int(actor.id),
    )
    return await _to_l2_view(session, row, parent, fetch_role_name=True)
