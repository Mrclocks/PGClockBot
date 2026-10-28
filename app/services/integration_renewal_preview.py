"""Informational live renewal projections do not reserve capacity or persist targets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_read_models import IntegrationCatalogPlan
from app.db.models import UserService
from app.services.integration_execution_policy import current_plan
from app.services.integration_observations import PanelIdentity, ServiceObservation, observe_service
from app.services.integration_pg import integration_pg_client
from app.services.integration_policy import IntegrationError
from app.services.integration_service_reads import QuotaView, ReadAccess, mapped_customer, read_authority, scoped_services
from app.services.service_renewals import calculate_renewal


@dataclass(frozen=True, slots=True)
class RenewalPreview:
    service_id: str
    customer_id: str
    plan_id: str
    terms_revision: str
    calculated_at: datetime
    current: QuotaView
    projected: QuotaView
    warnings: tuple[str, ...]


def quota_projection(value: ServiceObservation) -> QuotaView:
    return QuotaView(value.limit_bytes, value.used_bytes, max(0, value.limit_bytes-value.used_bytes) if value.limit_bytes else None,
        value.expires_at, value.on_hold_seconds, value.volume_unlimited, value.time_unlimited)


async def renewal_preview(session: AsyncSession, access: ReadAccess, customer_id: str, service_id: str, plan_id: str) -> RenewalPreview:
    scopes = frozenset({"services:read", "services:renew", "plans:read"})
    authority = await read_authority(session, access, scopes)
    customer = await mapped_customer(session, authority, customer_id)
    row = (await session.execute(scoped_services(authority, customer.id).where(UserService.public_id == service_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    service, snapshot, _catalog = row
    if snapshot is None or snapshot.state != "ready" or not service.pg_user_id:
        raise IntegrationError("access_not_ready")
    local_plan_id = await session.scalar(select(IntegrationCatalogPlan.plan_id).where(
        IntegrationCatalogPlan.integration_id == authority.integration_id, IntegrationCatalogPlan.public_id == plan_id))
    if local_plan_id is None:
        raise IntegrationError("plan_ineligible")
    identity = PanelIdentity(service.pg_username, authority.owner.context.pg_admin_username or "", service.pg_user_id)
    client = await integration_pg_client(session, authority)
    await session.rollback()
    async with asyncio.timeout(3):
        info, admin, groups = await asyncio.gather(client.request("GET", f"/api/user/by-id/{identity.panel_id}", use_cache=False),
            client.request("GET", "/api/admin", use_cache=False), client.get_groups_simple())
    observed = observe_service(info, identity)
    authority = await read_authority(session, access, scopes)
    await mapped_customer(session, authority, customer_id)
    terms = await current_plan(session, authority, local_plan_id, admin, groups)
    now = datetime.now(timezone.utc)
    result = calculate_renewal(info, {"volume_bytes": terms.data_limit_bytes, "duration_seconds": terms.duration_seconds or 0}, now=int(now.timestamp()))
    projected = observe_service({**info, **result["payload"]}, identity)
    warnings = []
    if observed.volume_unlimited and terms.data_limit_bytes:
        warnings.append("volume_becomes_finite")
    if observed.time_unlimited and terms.duration_seconds:
        warnings.append("time_becomes_finite")
    if observed.on_hold_seconds and not terms.duration_seconds:
        warnings.append("activates_on_hold")
    return RenewalPreview(service_id, customer_id, plan_id, terms.revision(), now,
        quota_projection(observed), quota_projection(projected), tuple(warnings))
