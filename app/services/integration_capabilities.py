"""Current, secret-free capability intersection with optional verified panel evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.integration_models import Integration
from app.db.models import ResellerProfile
from app.services.integration_authority import IntegrationAuthority, IntegrationPGEvidence, require_pg_evidence, resolve_integration_authority
from app.services.integration_policy import IntegrationError, PG_SCOPE_ACTIONS
from app.services.integration_rate_limits import LIMITS


@dataclass(frozen=True, slots=True)
class CapabilityLimits:
    page_max: int = 100
    read_per_minute: int = LIMITS["read"]
    writes_per_minute: int = LIMITS["write"]
    access_per_minute: int = LIMITS["private"]


@dataclass(frozen=True, slots=True)
class CapabilitiesView:
    api_version: str
    scopes: tuple[str, ...]
    operations: tuple[str, ...]
    customer_entry_policy: str
    billing_mode: str
    limits: CapabilityLimits
    authorization_synced_at: datetime | None


async def integration_capabilities(session: AsyncSession, bearer: str,
                                   verifier: Callable[[IntegrationAuthority], Awaitable[IntegrationPGEvidence]]) -> CapabilitiesView:
    authority = await resolve_integration_authority(session, bearer)
    await session.rollback()
    evidence = None
    try:
        evidence = await verifier(authority)
    except IntegrationError:
        evidence = None
    authority = await resolve_integration_authority(session, bearer)
    scopes = set(authority.local_scopes)
    for scope in scopes & PG_SCOPE_ACTIONS.keys():
        try:
            require_pg_evidence(authority, frozenset({scope}), evidence)
        except IntegrationError:
            scopes.discard(scope)
    policy = await session.scalar(select(Integration.customer_entry_policy).where(Integration.id == authority.integration_id))
    if policy != "website":
        scopes.discard("customers:write")
        scopes -= {"services:provision", "services:renew", "services:addons"}
    billing_mode = "platform" if authority.shop.shop_id is None else await session.scalar(select(ResellerProfile.billing_mode).where(
        ResellerProfile.user_id == authority.shop.shop_id,
    ))
    if not get_settings().integration_execution_enabled:
        scopes -= {"services:provision", "services:renew", "services:addons"}
    operations = tuple(kind for kind, scope in (("provision", "services:provision"), ("renew", "services:renew"), ("addon", "services:addons"))
                       if scope in scopes and "operations:read" in scopes)
    return CapabilitiesView("v1", tuple(sorted(scopes)), operations, policy, billing_mode or "fixed",
                            CapabilityLimits(), evidence.observed_at if evidence else None)
