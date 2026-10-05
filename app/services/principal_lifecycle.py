"""Phase 2D — Level-1 OrgPrincipal lifecycle (Owner-only service foundation).

Owner may list / view / disable / re-enable depth-1 Principals under themselves.
No UI, no bots, no depth-2, no automatic provisioning.

Hierarchy fields (parent_id / depth) are immutable via this API. Exactly one
active Owner remains. Soft-disable is preferred over hard delete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    OrgPrincipal,
    OrgPrincipalProvision,
    OrgPrincipalWebIdentity,
    Ticket,
    UserService,
)
from app.services.authz import authz_from_staff, is_explicit_org_owner
from app.services.org_principals import (
    DEPTH_ONE,
    DEPTH_OWNER,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    get_principal,
    is_owner_principal,
)
from app.services.platform_identity import is_explicit_owner_staff

log = logging.getLogger(__name__)


class PrincipalLifecycleError(Exception):
    """Denied or invalid Level-1 lifecycle operation."""

    def __init__(self, message: str, *, code: str = "denied"):
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Level1PrincipalView:
    """Safe Owner-facing summary — never carries credentials."""

    principal_id: int
    status: str
    parent_id: int | None
    depth: int
    pg_username: str | None
    pg_role_id: int | None
    pg_role_name: str | None
    web_identity_status: str  # none | active | inactive
    created_at: datetime | None

    def to_public_dict(self) -> dict[str, Any]:
        """API-safe dict. Explicitly omits password / enc / key material."""
        return {
            "principal_id": self.principal_id,
            "status": self.status,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "pg_username": self.pg_username,
            "pg_role_id": self.pg_role_id,
            "pg_role_name": self.pg_role_name,
            "web_identity_status": self.web_identity_status,
            "created_at": (
                self.created_at.isoformat() if self.created_at is not None else None
            ),
        }


def assert_owner_lifecycle_actor(staff: Mapping[str, Any] | None) -> None:
    """AUTHENTICATED ∧ ACTIVE OWNER PRINCIPAL — Level-1 siblings DENY."""
    if not staff:
        raise PrincipalLifecycleError("احراز هویت نشده", code="unauthenticated")
    if not is_explicit_owner_staff(staff):
        raise PrincipalLifecycleError(
            "فقط مالک صریح سازمان می‌تواند Principal سطح ۱ را مدیریت کند",
            code="not_owner",
        )
    ctx = authz_from_staff(staff)
    if not is_explicit_org_owner(ctx):
        raise PrincipalLifecycleError(
            "مالک سازمان فعال نیست",
            code="inactive_owner",
        )


def _owner_id_from_staff(staff: Mapping[str, Any]) -> int:
    try:
        pid = int(staff.get("org_principal_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        raise PrincipalLifecycleError("مالک سازمان فعال نیست", code="inactive_owner")
    return pid


async def _load_owner_actor(
    session: AsyncSession, staff: Mapping[str, Any] | None
) -> OrgPrincipal:
    assert_owner_lifecycle_actor(staff)
    assert staff is not None
    owner = await get_principal(session, _owner_id_from_staff(staff))
    if owner is None or not is_owner_principal(owner):
        raise PrincipalLifecycleError(
            "مالک سازمان فعال نیست",
            code="inactive_owner",
        )
    return owner


async def _web_identity_status(
    session: AsyncSession, principal_id: int
) -> str:
    result = await session.execute(
        select(OrgPrincipalWebIdentity).where(
            OrgPrincipalWebIdentity.principal_id == int(principal_id)
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return "none"
    return "active" if bool(row.is_active) else "inactive"


async def _provision_role_meta(
    session: AsyncSession, principal_id: int
) -> tuple[int | None, str | None]:
    """Latest completed provision role id (name filled separately if safe)."""
    result = await session.execute(
        select(OrgPrincipalProvision)
        .where(OrgPrincipalProvision.principal_id == int(principal_id))
        .order_by(OrgPrincipalProvision.id.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None or row.pg_role_id is None:
        return None, None
    return int(row.pg_role_id), None


async def _safe_pg_role_name(role_id: int | None, *, client: Any | None = None) -> str | None:
    """Best-effort role *name* for display only — never implies hierarchy.

    Never uses Owner ``get_pg()`` implicitly. Caller must pass the allowed
    Principal/parent client (Owner may pass ``get_pg()`` explicitly).
    """
    if role_id is None or client is None:
        return None
    try:
        roles = await client.get_admin_roles()
    except Exception:
        return None
    if not isinstance(roles, list):
        return None
    for role in roles:
        if not isinstance(role, dict):
            continue
        try:
            rid = int(role.get("id"))
        except (TypeError, ValueError):
            continue
        if rid == int(role_id):
            name = role.get("name")
            return str(name) if name is not None else None
    return None


async def _to_view(
    session: AsyncSession,
    row: OrgPrincipal,
    *,
    fetch_role_name: bool = False,
) -> Level1PrincipalView:
    role_id, role_name = await _provision_role_meta(session, int(row.id))
    if fetch_role_name and role_id is not None and role_name is None:
        from app.services.pasarguard import get_pg

        # Owner-only L1 lifecycle — explicit Owner client for display metadata.
        role_name = await _safe_pg_role_name(role_id, client=get_pg())
    web_status = await _web_identity_status(session, int(row.id))
    return Level1PrincipalView(
        principal_id=int(row.id),
        status=str(row.status),
        parent_id=int(row.parent_id) if row.parent_id is not None else None,
        depth=int(row.depth),
        pg_username=(str(row.pg_username).strip() if row.pg_username else None) or None,
        pg_role_id=role_id,
        pg_role_name=role_name,
        web_identity_status=web_status,
        created_at=getattr(row, "created_at", None),
    )


async def _assert_managed_level1(
    session: AsyncSession,
    owner: OrgPrincipal,
    principal_id: int,
) -> OrgPrincipal:
    """Target must be depth-1 under this Owner — never Owner, never sibling scope."""
    try:
        pid = int(principal_id)
    except (TypeError, ValueError) as exc:
        raise PrincipalLifecycleError(
            "شناسه Principal نامعتبر است",
            code="invalid_id",
        ) from exc
    if pid <= 0:
        raise PrincipalLifecycleError(
            "شناسه Principal نامعتبر است",
            code="invalid_id",
        )
    if pid == int(owner.id):
        raise PrincipalLifecycleError(
            "مالک سازمان قابل مدیریت از این مسیر نیست",
            code="cannot_manage_owner",
        )
    row = await get_principal(session, pid)
    if row is None:
        raise PrincipalLifecycleError(
            "Principal یافت نشد",
            code="not_found",
        )
    if int(row.depth) != DEPTH_ONE:
        raise PrincipalLifecycleError(
            "فقط Principal سطح ۱ قابل مدیریت است",
            code="not_level1",
        )
    if row.parent_id is None or int(row.parent_id) != int(owner.id):
        raise PrincipalLifecycleError(
            "این Principal در محدوده مالک نیست",
            code="out_of_scope",
        )
    return row


async def list_level1_principals(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
) -> list[Level1PrincipalView]:
    """Owner lists own Level-1 children (active + disabled). Non-Owner → DENY."""
    owner = await _load_owner_actor(session, staff)
    result = await session.execute(
        select(OrgPrincipal)
        .where(
            OrgPrincipal.parent_id == int(owner.id),
            OrgPrincipal.depth == DEPTH_ONE,
        )
        .order_by(OrgPrincipal.id.asc())
    )
    rows = list(result.scalars().all())
    return [await _to_view(session, r, fetch_role_name=False) for r in rows]


async def get_level1_principal_detail(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    principal_id: int,
) -> Level1PrincipalView:
    """Owner detail for one Level-1 child. Non-Owner / sibling → DENY."""
    owner = await _load_owner_actor(session, staff)
    row = await _assert_managed_level1(session, owner, principal_id)
    return await _to_view(session, row, fetch_role_name=True)


def _invalidate_pg_cache(principal_id: int) -> None:
    try:
        from app.services.pasarguard import invalidate_pg_principal_cache

        invalidate_pg_principal_cache(principal_id)
    except Exception:
        log.error(
            "Phase 2D: failed to invalidate PG cache for principal_id=%s",
            int(principal_id),
        )


async def disable_level1_principal(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    principal_id: int,
) -> Level1PrincipalView:
    """Soft-disable: status → disabled. Does not delete PG account or resources.

    Immediately blocks Web login / session resolve / PG client selection (DB status).
    """
    owner = await _load_owner_actor(session, staff)
    row = await _assert_managed_level1(session, owner, principal_id)
    if is_owner_principal(row):
        raise PrincipalLifecycleError(
            "نمی‌توان مالک را غیرفعال کرد",
            code="cannot_disable_owner",
        )
    if int(row.depth) == DEPTH_OWNER or row.parent_id is None:
        raise PrincipalLifecycleError(
            "نمی‌توان مالک را غیرفعال کرد",
            code="cannot_disable_owner",
        )
    if str(row.status) == STATUS_DISABLED:
        return await _to_view(session, row, fetch_role_name=True)

    row.status = STATUS_DISABLED
    await session.flush()
    _invalidate_pg_cache(int(row.id))
    log.info(
        "Phase 2D: disabled Level-1 principal_id=%s by owner_id=%s",
        int(row.id),
        int(owner.id),
    )
    return await _to_view(session, row, fetch_role_name=True)


async def enable_level1_principal(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    principal_id: int,
) -> Level1PrincipalView:
    """Re-enable a disabled Level-1 Principal — same id / parent / depth / PG identity."""
    owner = await _load_owner_actor(session, staff)
    row = await _assert_managed_level1(session, owner, principal_id)
    # Re-assert immutable hierarchy (never promote to Owner on enable).
    if int(row.depth) != DEPTH_ONE or row.parent_id is None:
        raise PrincipalLifecycleError(
            "سلسله‌مراتب Principal نامعتبر است",
            code="hierarchy_invalid",
        )
    if int(row.parent_id) != int(owner.id):
        raise PrincipalLifecycleError(
            "این Principal در محدوده مالک نیست",
            code="out_of_scope",
        )
    if is_owner_principal(row):
        raise PrincipalLifecycleError(
            "نمی‌توان Principal را به مالک تبدیل کرد",
            code="cannot_become_owner",
        )
    if str(row.status) == STATUS_ACTIVE:
        return await _to_view(session, row, fetch_role_name=True)

    row.status = STATUS_ACTIVE
    await session.flush()
    log.info(
        "Phase 2D: re-enabled Level-1 principal_id=%s by owner_id=%s",
        int(row.id),
        int(owner.id),
    )
    return await _to_view(session, row, fetch_role_name=True)


# Soft deactivate is the preferred remove-access path (alias of disable).
deactivate_level1_principal = disable_level1_principal


async def count_owned_resources(
    session: AsyncSession, principal_id: int
) -> int:
    """Count business resources with explicit owner_principal_id == principal."""
    pid = int(principal_id)
    total = 0
    for model in (BotUser, Order, Ticket, UserService):
        col = getattr(model, "owner_principal_id", None)
        if col is None:
            continue
        n = await session.scalar(
            select(func.count()).select_from(model).where(col == pid)
        )
        total += int(n or 0)
    return total


async def hard_delete_level1_principal(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    principal_id: int,
    *,
    confirm: bool = False,
) -> None:
    """Hard delete only when explicit + no owned resources. Prefer soft-disable.

    Does not delete the PasarGuard admin account. Does not reassign resources
    to Owner. Unknown/owned resources → fail closed.
    """
    if not confirm:
        raise PrincipalLifecycleError(
            "حذف قطعی نیاز به تأیید صریح دارد — ترجیحاً غیرفعال‌سازی نرم",
            code="confirm_required",
        )
    owner = await _load_owner_actor(session, staff)
    row = await _assert_managed_level1(session, owner, principal_id)
    if is_owner_principal(row) or int(row.depth) == DEPTH_OWNER:
        raise PrincipalLifecycleError(
            "نمی‌توان مالک را حذف کرد",
            code="cannot_delete_owner",
        )
    owned = await count_owned_resources(session, int(row.id))
    if owned > 0:
        raise PrincipalLifecycleError(
            "Principal دارای منبع وابسته است — حذف قطعی مجاز نیست",
            code="owns_resources",
        )
    # Prefer soft-disable first; hard delete only after disabled.
    if str(row.status) == STATUS_ACTIVE:
        raise PrincipalLifecycleError(
            "ابتدا Principal را غیرفعال کنید",
            code="must_disable_first",
        )

    # Fail closed when depth-2 children still reference this parent.
    child_n = await session.scalar(
        select(func.count())
        .select_from(OrgPrincipal)
        .where(OrgPrincipal.parent_id == int(row.id))
    )
    if int(child_n or 0) > 0:
        raise PrincipalLifecycleError(
            "Principal دارای فرزند وابسته است — ابتدا فرزندان را غیرفعال/حذف کنید",
            code="has_children",
        )

    # Detach web identity rows (no orphan login mapping).
    identities = (
        await session.execute(
            select(OrgPrincipalWebIdentity).where(
                OrgPrincipalWebIdentity.principal_id == int(row.id)
            )
        )
    ).scalars().all()
    for identity in identities:
        await session.delete(identity)

    # Provision ledger rows reference principal_id / created_by — remove both.
    provisions = (
        await session.execute(
            select(OrgPrincipalProvision).where(
                (OrgPrincipalProvision.principal_id == int(row.id))
                | (OrgPrincipalProvision.created_by_principal_id == int(row.id))
            )
        )
    ).scalars().all()
    for prov in provisions:
        await session.delete(prov)

    # Detach adapter FKs so a later ResellerProfile / PgStaff delete is safe
    # if this principal was the only remaining link.
    row.reseller_profile_id = None
    row.pg_staff_id = None
    row.bot_user_id = None
    await session.flush()

    pid = int(row.id)
    await session.delete(row)
    await session.flush()
    _invalidate_pg_cache(pid)
    log.info(
        "Phase 2D: hard-deleted Level-1 principal_id=%s by owner_id=%s",
        pid,
        int(owner.id),
    )


async def reject_hierarchy_mutation(
    staff: Mapping[str, Any] | None,
    *,
    principal_id: int | None = None,
    parent_id: Any = None,
    depth: Any = None,
    promote_to_owner: bool = False,
) -> None:
    """Explicit deny for client-controlled parent/depth / Owner promotion.

    Always raises — hierarchy is server-only and immutable for Level-1.
    """
    _ = principal_id
    # Still require Owner for management attempts; Level-1 also DENY.
    if staff is not None:
        try:
            assert_owner_lifecycle_actor(staff)
        except PrincipalLifecycleError:
            raise PrincipalLifecycleError(
                "تغییر والد/عمق مجاز نیست",
                code="hierarchy_immutable",
            )
    if promote_to_owner or depth == DEPTH_OWNER or parent_id is None and depth == 0:
        raise PrincipalLifecycleError(
            "تبدیل به مالک یا ایجاد مالک دوم مجاز نیست",
            code="cannot_become_owner",
        )
    raise PrincipalLifecycleError(
        "تغییر والد/عمق مجاز نیست",
        code="hierarchy_immutable",
    )


def public_views_contain_secret(views: list[Level1PrincipalView] | Level1PrincipalView) -> bool:
    """True if any public dict accidentally carries credential material."""
    items = views if isinstance(views, list) else [views]
    banned_keys = {
        "password",
        "pg_password",
        "pg_password_enc",
        "web_password",
        "panel_password",
        "web_password_hash",
        "encryption_key",
        "web_secret",
    }
    for view in items:
        data = view.to_public_dict()
        if banned_keys & set(data.keys()):
            return True
        blob = str(data).lower()
        if "password_enc" in blob or "gAAAAA" in str(data):
            return True
    return False
