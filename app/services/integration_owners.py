"""Resolve current eligible integration owners from server-owned principal records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, OrgPrincipal, ResellerProfile
from app.services.authz import AuthzContext, authz_from_staff, can_shop
from app.services.integration_policy import IntegrationError, IntegrationShop, ScopeKind
from app.services.org_principals import (
    OrgPrincipalError,
    attach_org_principal_fields,
    is_owner_principal,
    resolve_org_principal_for_staff,
    validate_parent_depth,
)
from app.services.resellers import parse_perms, setup_is_complete
from app.services.shop_scope import resolve_shop_scope_id


@dataclass(frozen=True, slots=True)
class IntegrationOwner:
    principal_id: int
    shop: IntegrationShop
    context: AuthzContext


async def _fresh_principal(session: AsyncSession, principal_id: int) -> OrgPrincipal | None:
    return await session.scalar(
        select(OrgPrincipal).where(OrgPrincipal.id == principal_id).execution_options(populate_existing=True)
    )


async def _shop_staff(session: AsyncSession, principal: OrgPrincipal) -> dict[str, Any]:
    profile = await session.scalar(
        select(ResellerProfile).where(ResellerProfile.id == principal.reseller_profile_id)
        .execution_options(populate_existing=True)
    )
    if not setup_is_complete(profile):
        raise IntegrationError("principal_inactive")
    assert profile is not None
    user = await session.scalar(
        select(BotUser).where(BotUser.id == profile.user_id).execution_options(populate_existing=True)
    )
    if user is None or user.is_blocked or principal.bot_user_id not in (None, user.id):
        raise IntegrationError("principal_inactive")
    if not principal.pg_username or not (principal.pg_api_key_enc or principal.pg_password_enc):
        raise IntegrationError("principal_inactive")
    if (profile.pg_admin_username or "").strip() != principal.pg_username.strip():
        raise IntegrationError("principal_inactive")
    return {
        "role": "principal",
        "reseller_profile_id": profile.id,
        "bot_user_id": user.id,
        "permissions": parse_perms(profile.web_permissions),
        "pg_admin_username": principal.pg_username.strip(),
    }


async def load_integration_owner(session: AsyncSession, principal_id: int) -> IntegrationOwner:
    principal = await _fresh_principal(session, principal_id)
    if principal is None or principal.status != "active":
        raise IntegrationError("principal_inactive")
    if is_owner_principal(principal):
        # Owner authority comes from the explicit principal, never an admin role.
        staff: dict[str, Any] = {"role": "principal", "pg_admin_username": principal.pg_username}
        scope_kind = ScopeKind.PLATFORM
    else:
        if principal.depth not in (1, 2) or principal.parent_id is None:
            raise IntegrationError("principal_inactive")
        parent = await _fresh_principal(session, principal.parent_id)
        try:
            validate_parent_depth(depth=principal.depth, parent=parent)
        except OrgPrincipalError:
            raise IntegrationError("principal_inactive") from None
        staff = await _shop_staff(session, principal)
        scope_kind = ScopeKind.SHOP
    staff = attach_org_principal_fields(staff, principal, visible_principal_ids=frozenset({principal.id}))
    shop = IntegrationShop(scope_kind, resolve_shop_scope_id(staff))
    return IntegrationOwner(principal.id, shop, authz_from_staff(staff))


async def management_owner(session: AsyncSession, authenticated_staff: dict[str, Any]) -> IntegrationOwner:
    """Caller must supply staff authenticated by the existing panel dependencies."""
    principal = await resolve_org_principal_for_staff(session, authenticated_staff)
    if principal is None:
        raise IntegrationError("permission_denied")
    owner = await load_integration_owner(session, principal.id)
    if not can_shop(owner.context, "integrations_manage"):
        raise IntegrationError("permission_denied")
    return owner
