"""Resolve uncached local authority; require separate identity-bound PG evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_models import Integration
from app.db.models import Setting
from app.services.authz import can_shop
from app.services.integration_credentials import verify_credential
from app.services.integration_owners import IntegrationOwner, load_integration_owner
from app.services.integration_policy import (
    PG_SCOPE_ACTIONS, SCOPE_FEATURES, IntegrationError, IntegrationShop, stored_scopes, validate_scopes,
)
from app.services.integrations import allowed_integration_plans, integration_shop
from app.services.principal_pg_authz import authorize_pg_action, authorize_pg_user_action

PG_EVIDENCE_MAX_AGE = timedelta(minutes=5)
LIVE_PG_SCOPES = frozenset({"services:provision", "services:renew", "services:addons"})


@dataclass(frozen=True, slots=True)
class IntegrationAuthority:
    integration_id: str
    credential_selector: str
    owner: IntegrationOwner
    shop: IntegrationShop
    local_scopes: frozenset[str]
    allowed_plan_ids: frozenset[int]
    pg_evidence: IntegrationPGEvidence | None = None

    def require_resource(self, *, integration_id: str, shop: IntegrationShop) -> None:
        if self.integration_id != integration_id or self.shop != shop:
            raise IntegrationError("not_found")

    def require_plan(self, plan_id: int) -> None:
        if type(plan_id) is not int or plan_id not in self.allowed_plan_ids:
            raise IntegrationError("plan_ineligible")


@dataclass(frozen=True, slots=True)
class IntegrationPGEvidence:
    """An injected verifier's observation, never constructed from request fields.

    No evidence is fetched or cached by the foundation. Dispatch will also need
    the live quota/billing/provisioning gates in later phases.
    """

    principal_id: int
    pg_username: str
    observed_at: datetime
    is_live: bool
    user_actions: frozenset[str] = frozenset()
    catalog_actions: frozenset[str] = frozenset()
    rejected: bool = False


def require_local_scopes(authority: IntegrationAuthority, required: frozenset[str]) -> None:
    if not validate_scopes(required) <= authority.local_scopes:
        raise IntegrationError("permission_denied")


def require_pg_evidence(
    authority: IntegrationAuthority, required: frozenset[str], evidence: IntegrationPGEvidence | None,
) -> None:
    pg_scopes = required & PG_SCOPE_ACTIONS.keys()
    if not pg_scopes:
        return
    if evidence is None or evidence.observed_at.tzinfo is None:
        raise IntegrationError("authorization_unavailable")
    context = authority.owner.context
    if evidence.principal_id != authority.owner.principal_id or not evidence.pg_username:
        raise IntegrationError("authorization_unavailable")
    if evidence.pg_username != context.pg_admin_username:
        raise IntegrationError("authorization_unavailable")
    age = datetime.now(timezone.utc) - evidence.observed_at
    if age < timedelta(0) or age > PG_EVIDENCE_MAX_AGE or (required & LIVE_PG_SCOPES and not evidence.is_live):
        raise IntegrationError("authorization_unavailable")
    if evidence.rejected:
        raise IntegrationError("permission_denied")
    staff = {
        "role": "principal", "org_principal_id": context.principal_id, "org_depth": context.depth,
        "org_parent_id": context.parent_id, "org_status": context.principal_status,
        "org_visible_principal_ids": [context.principal_id],
        "pg_admin_username": evidence.pg_username, "pg_credentials_ready": True, "pg_capabilities_ok": True,
        "pg_user_actions": {action: True for action in evidence.user_actions},
        "pg_actions": {resource: {"read": True} for resource in evidence.catalog_actions},
    }
    for scope in pg_scopes:
        if scope == "plans:read":
            allowed = all(authorize_pg_action(staff, resource, "read").allowed for resource in ("templates", "groups"))
        else:
            allowed = all(authorize_pg_user_action(staff, action).allowed for action in PG_SCOPE_ACTIONS[scope])
        if not allowed:
            raise IntegrationError("permission_denied")


async def resolve_integration_authority(
    session: AsyncSession, bearer: str, *, required_scopes: frozenset[str] = frozenset(),
    pg_evidence: IntegrationPGEvidence | None = None,
) -> IntegrationAuthority:
    # Read the global switch directly, without shop overlays or settings caches.
    enabled = await session.scalar(select(Setting.value).where(Setting.key == "integration_api_enabled"))
    if enabled != "1":
        raise IntegrationError("api_disabled")
    credential = await verify_credential(session, bearer)
    integration = await session.scalar(select(Integration).where(
        Integration.id == credential.integration_id,
    ).execution_options(populate_existing=True))
    if integration is None or integration.status != "active":
        raise IntegrationError("integration_disabled")
    owner = await load_integration_owner(session, integration.owner_principal_id)
    shop = integration_shop(integration)
    if owner.shop != shop:
        raise IntegrationError("permission_denied")
    candidates = stored_scopes(credential.scopes) & stored_scopes(integration.grants)
    local_scopes = frozenset(scope for scope in candidates if all(can_shop(owner.context, key) for key in SCOPE_FEATURES[scope]))
    if "services:read" not in local_scopes:
        local_scopes -= {"services:access"}
    # Capabilities disclose only this intersection and require authentication, not a grant.
    authority = IntegrationAuthority(
        integration.id, credential.selector, owner, shop, local_scopes | {"capabilities:read"},
        await allowed_integration_plans(session, integration), pg_evidence,
    )
    require_local_scopes(authority, required_scopes)
    require_pg_evidence(authority, required_scopes, pg_evidence)
    return authority
