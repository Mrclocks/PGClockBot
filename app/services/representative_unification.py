"""Representative product: OrgPrincipal identity + ResellerProfile shop package.

Not a second hierarchy or permission engine. Parent/depth/ids from the
authenticated Principal only. PG ``admins.create`` is the sub-rep switch.
"""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrgPrincipal, ResellerProfile, Role
from app.services.org_principals import (
    DEPTH_ONE,
    DEPTH_TWO,
    STATUS_ACTIVE,
    get_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_child_provisioning import (
    ChildProvisionError,
    has_pg_admin_create_capability,
)
from app.services.resellers import DEFAULT_FEATURE_PERMS


class RepresentativeUnifyError(Exception):
    def __init__(self, message: str, *, code: str = "denied"):
        self.message = message
        self.code = code
        super().__init__(message)


def _positive_id(raw: Any) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def staff_is_sub_representative(staff: Mapping[str, Any] | None) -> bool:
    if not staff or is_explicit_owner_staff(staff):
        return False
    try:
        return int(staff.get("org_depth")) == DEPTH_TWO
    except (TypeError, ValueError):
        return False


def staff_can_manage_representatives(staff: Mapping[str, Any] | None) -> bool:
    """Owner with PG admin-create, or active Representative with real ``admins.create``."""
    if not staff:
        return False
    if staff_is_sub_representative(staff):
        return False
    if is_explicit_owner_staff(staff):
        from app.services.principal_provisioning import owner_has_pg_admin_create_capability

        return owner_has_pg_admin_create_capability(staff)
    try:
        depth = int(staff.get("org_depth")) if staff.get("org_depth") is not None else None
    except (TypeError, ValueError):
        depth = None
    if depth != DEPTH_ONE:
        return False
    if str(staff.get("org_status") or "") != STATUS_ACTIVE:
        return False
    if _positive_id(staff.get("org_principal_id")) <= 0:
        return False
    return has_pg_admin_create_capability(staff)


def assert_staff_can_manage_representatives(staff: Mapping[str, Any] | None) -> None:
    if not staff:
        raise RepresentativeUnifyError("احراز هویت نشده", code="unauthenticated")
    if staff_is_sub_representative(staff):
        raise RepresentativeUnifyError(
            "زیرنماینده نمی‌تواند نماینده بسازد",
            code="sub_representative_forbidden",
        )
    if not staff_can_manage_representatives(staff):
        raise RepresentativeUnifyError(
            "قابلیت ساخت نماینده برای این حساب فعال نیست",
            code="pg_capability_denied",
        )


async def assert_live_parent_for_child(
    session: AsyncSession, staff: Mapping[str, Any] | None
) -> OrgPrincipal:
    """Fail closed when the parent Principal is missing or disabled."""
    assert_staff_can_manage_representatives(staff)
    assert staff is not None
    if is_explicit_owner_staff(staff):
        from app.services.org_principals import ensure_owner_principal

        owner = await ensure_owner_principal(session)
        if owner is None or str(owner.status) != STATUS_ACTIVE:
            raise RepresentativeUnifyError("مالک سازمان فعال نیست", code="inactive_owner")
        return owner
    pid = _positive_id(staff.get("org_principal_id"))
    parent = await get_principal(session, pid) if pid else None
    if (
        parent is None
        or str(parent.status) != STATUS_ACTIVE
        or int(parent.depth) != DEPTH_ONE
        or is_owner_principal(parent)
    ):
        raise RepresentativeUnifyError("نماینده فعال نیست", code="inactive_or_missing_principal")
    return parent


async def attach_shop_package_to_principal(
    session: AsyncSession,
    child: OrgPrincipal,
    *,
    pg_username: str,
) -> ResellerProfile:
    """Independent shop/bot package for a Representative or Sub-Representative.

    PG secret stays on the Principal. Shop row points at the same PG username
    so delivery uses ``get_pg_for_principal`` via the reseller client router.
    Web login remains OrgPrincipalWebIdentity (no second username).
    """
    try:
        depth = int(child.depth) if child is not None else -1
    except (TypeError, ValueError):
        depth = -1
    if child is None or is_owner_principal(child) or depth not in (DEPTH_ONE, DEPTH_TWO):
        raise ChildProvisionError("Principal برای فروشگاه نامعتبر است", code="not_representative")
    if str(child.status) != STATUS_ACTIVE:
        raise ChildProvisionError("Principal فرزند فعال نیست", code="inactive_child")

    from app.services.resellers import get_or_create_pg_linked_bot_user, normalize_feature_perms

    user = await get_or_create_pg_linked_bot_user(session, pg_username)
    user.role = Role.RESELLER.value
    user.full_name = user.full_name or f"نماینده {pg_username}"

    existing = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(user.id))
        )
    ).scalar_one_or_none()
    perms = normalize_feature_perms(DEFAULT_FEATURE_PERMS)
    if existing is None:
        profile = ResellerProfile(
            user_id=int(user.id),
            can_approve_receipts=False,
            pg_admin_username=pg_username,
            pg_admin_password_enc=None,
            web_username=None,
            web_password_hash=None,
            web_permissions=perms,
            bot_permissions=perms,
            is_active=True,
            allow_volume_oversell=False,
        )
        session.add(profile)
        await session.flush()
    else:
        profile = existing
        profile.is_active = True
        profile.pg_admin_username = pg_username
        profile.web_permissions = perms
        profile.bot_permissions = perms
        await session.flush()

    if int(getattr(profile, "id", 0) or 0) <= 0:
        raise ChildProvisionError("بسته فروشگاه ساخته نشد", code="shop_attach_failed")

    mapped = (
        await session.execute(
            select(OrgPrincipal).where(
                OrgPrincipal.reseller_profile_id == int(profile.id),
                OrgPrincipal.id != int(child.id),
            )
        )
    ).scalar_one_or_none()
    if mapped is not None:
        raise ChildProvisionError(
            "این فروشگاه از قبل به نماینده دیگری وصل است",
            code="shop_identity_collision",
        )

    child.reseller_profile_id = int(profile.id)
    child.bot_user_id = int(user.id)
    await session.flush()
    return profile


async def descendant_shop_profile_ids(
    session: AsyncSession, staff: Mapping[str, Any] | None
) -> frozenset[int]:
    """ResellerProfile ids the actor may list. Owner: all mapped shops in scope."""
    if not staff:
        return frozenset()
    pid = _positive_id(staff.get("org_principal_id"))
    actor = await get_principal(session, pid) if pid else None
    if actor is None:
        return frozenset()
    visible = await visible_principal_ids(session, actor)
    result = await session.execute(
        select(OrgPrincipal).where(OrgPrincipal.id.in_(tuple(visible) or (0,)))
    )
    rows = list(result.scalars().all())
    if is_explicit_owner_staff(staff) or is_owner_principal(actor):
        return frozenset(
            int(p.reseller_profile_id)
            for p in rows
            if p.reseller_profile_id is not None
        )
    if int(actor.depth) != DEPTH_ONE:
        return frozenset()
    return frozenset(
        int(p.reseller_profile_id)
        for p in rows
        if p.reseller_profile_id is not None
        and int(p.parent_id or 0) == int(actor.id)
        and int(p.depth) == DEPTH_TWO
        and str(p.status) == STATUS_ACTIVE
    )


async def shop_bot_can_manage_representatives(
    session: AsyncSession,
    db_user,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    reseller_profile_id: int | None = None,
) -> bool:
    """Shop-bot actor may add a Sub-Representative only with real PG capability."""
    if not is_reseller_bot:
        return False
    from app.services.bot_principal_identity import resolve_bot_principal_bridge

    resolution = await resolve_bot_principal_bridge(
        session,
        db_user=db_user,
        is_reseller_bot=True,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if resolution is None:
        return False
    return staff_can_manage_representatives(resolution.staff)


# Backward-compatible alias used by L2 provision.
attach_shop_package_to_child_principal = attach_shop_package_to_principal
