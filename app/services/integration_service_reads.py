"""Customer-qualified service projections and bounded optional panel refreshes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer, IntegrationCustomer
from app.db.integration_operation_models import ServiceMutationGuard
from app.db.integration_models import Integration
from app.db.integration_read_models import IntegrationCatalogPlan, IntegrationServiceSnapshot
from app.db.models import UserService
from app.services.integration_authority import IntegrationAuthority, IntegrationPGEvidence, resolve_integration_authority
from app.services.integration_credentials import verify_credential
from app.services.integration_events import service_event
from app.services.integration_customers import _customer_query
from app.services.integration_observations import PanelIdentity, ServiceObservation, observe_service
from app.services.integration_pagination import CursorBinding, IntegrationCursors, Keyset, page_limit
from app.services.integration_pg import integration_pg_client
from app.services.integration_policy import IntegrationError, utc_time
from app.services.pasarguard import PasarGuardError
from app.services.pg_read_resilience import web_read_budget

logger = logging.getLogger(__name__)

FRESH_TTL = timedelta(seconds=60)
_REFRESH_LIMITS: dict[str, asyncio.Semaphore] = {}
_OVERFLOW_REFRESH_LIMIT = asyncio.Semaphore(4)


@dataclass(frozen=True, slots=True)
class ReadAccess:
    bearer: str = field(repr=False)
    evidence: IntegrationPGEvidence | None = None


@dataclass(frozen=True, slots=True)
class ServiceSelection:
    customer_id: str
    cursor: str = ""
    limit: int | str = 25


@dataclass(frozen=True, slots=True)
class QuotaView:
    limit_bytes: int | None
    used_bytes: int | None
    remaining_bytes: int | None
    expires_at: datetime | None
    on_hold_seconds: int | None
    volume_unlimited: bool | None
    time_unlimited: bool | None


@dataclass(frozen=True, slots=True)
class FreshnessView:
    source: str
    synced_at: datetime | None
    freshness: str
    panel_available: bool | None


@dataclass(frozen=True, slots=True)
class ServiceView:
    id: str
    customer_id: str
    plan_id: str | None
    state: str
    panel_status: str
    quota: QuotaView
    freshness: FreshnessView
    resource_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ServicePage:
    items: tuple[ServiceView, ...]
    next_cursor: str | None
    has_more: bool


async def read_authority(session: AsyncSession, access: ReadAccess, scopes: frozenset[str]) -> IntegrationAuthority:
    return await resolve_integration_authority(session, access.bearer, required_scopes=scopes, pg_evidence=access.evidence)


async def mapped_customer(session: AsyncSession, authority: IntegrationAuthority, customer_id: str) -> Customer:
    row = (await session.execute(_customer_query(authority).where(Customer.public_id == customer_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    if row[0].state != "active":
        raise IntegrationError("customer_blocked")
    return row[0]


def scoped_services(authority: IntegrationAuthority, customer_id: int) -> Select:
    return select(UserService, IntegrationServiceSnapshot, IntegrationCatalogPlan.public_id).join(
        IntegrationCustomer, (IntegrationCustomer.customer_id == UserService.customer_id)
        & (IntegrationCustomer.integration_id == UserService.integration_id),
    ).join(Integration, Integration.id == UserService.integration_id).join(
        Customer, Customer.id == UserService.customer_id,
    ).outerjoin(
        IntegrationServiceSnapshot, IntegrationServiceSnapshot.service_id == UserService.id,
    ).outerjoin(IntegrationCatalogPlan, (IntegrationCatalogPlan.integration_id == UserService.integration_id)
                & (IntegrationCatalogPlan.plan_id == UserService.plan_id)).where(
        Customer.state == "active", Customer.legacy_bot_user_id.is_(None),
        UserService.integration_id == authority.integration_id, UserService.customer_id == customer_id,
        UserService.bot_user_id.is_(None), UserService.origin_operation_id.is_not(None),
        UserService.shop_id == authority.shop.shop_id, UserService.shop_needs_review.is_(False),
        UserService.owner_principal_id == authority.owner.principal_id,
        Integration.owner_principal_id == authority.owner.principal_id,
        Integration.shop_id == authority.shop.shop_id, Integration.scope_kind == authority.shop.kind.value,
    ).execution_options(populate_existing=True)


def service_view(service: UserService, snapshot: IntegrationServiceSnapshot | None, plan_id: str | None,
                 customer_id: str, *, source: str = "local") -> ServiceView:
    if snapshot is None:
        quota = QuotaView(None, None, None, None, None, None, None)
        freshness = FreshnessView(source, None, "unknown", None)
        state, panel_status, version = "review", "unknown", 1
    else:
        limit, used = snapshot.limit_bytes, snapshot.used_bytes
        remaining = max(0, limit - used) if limit is not None and used is not None else None
        quota = QuotaView(limit, used, remaining, utc_time(snapshot.expires_at) if snapshot.expires_at else None,
                          snapshot.on_hold_seconds, snapshot.volume_unlimited, snapshot.time_unlimited)
        synced = utc_time(snapshot.synced_at) if snapshot.synced_at else None
        age = datetime.now(timezone.utc) - synced if synced else None
        freshness = FreshnessView(source, synced, "unknown" if age is None else
            ("fresh" if timedelta(0) <= age <= FRESH_TTL else "stale"), snapshot.panel_available)
        state, panel_status, version = snapshot.state, snapshot.panel_status, snapshot.resource_version
    return ServiceView(service.public_id, customer_id, plan_id, "cancelled" if service.is_cancelled else state,
                       panel_status, quota, freshness, version, utc_time(service.created_at))


async def list_integration_services(session: AsyncSession, access: ReadAccess, selection: ServiceSelection,
                                    cursors: IntegrationCursors) -> ServicePage:
    limit = page_limit(selection.limit)
    authority = await read_authority(session, access, frozenset({"services:read"}))
    customer = await mapped_customer(session, authority, selection.customer_id)
    query = scoped_services(authority, customer.id)
    binding = CursorBinding(authority.integration_id, authority.shop.shop_id, selection.customer_id, "services")
    if selection.cursor:
        key = cursors.decode(selection.cursor, binding)
    else:
        high = await session.scalar(query.with_only_columns(UserService.id).order_by(UserService.id.desc()).limit(1))
        key = Keyset((high or 0) + 1, high or 0)
    rows = (await session.execute(query.where(UserService.id <= key.high, UserService.id < key.last)
                                  .order_by(UserService.id.desc()).limit(limit + 1))).all()
    visible = rows[:limit]
    has_more = len(rows) > limit
    cursor = cursors.encode(binding, Keyset(visible[-1][0].id, key.high)) if has_more else None
    return ServicePage(tuple(service_view(*row, selection.customer_id) for row in visible), cursor, has_more)


def _refresh_limit(integration_id: str) -> asyncio.Semaphore:
    if integration_id not in _REFRESH_LIMITS:
        if len(_REFRESH_LIMITS) >= 256:
            return _OVERFLOW_REFRESH_LIMIT
        _REFRESH_LIMITS[integration_id] = asyncio.Semaphore(4)
    return _REFRESH_LIMITS[integration_id]


def observation_values(observation: ServiceObservation, now: datetime) -> dict:
    return {"panel_status": observation.status, "limit_bytes": observation.limit_bytes,
            "used_bytes": observation.used_bytes, "expires_at": observation.expires_at,
            "on_hold_seconds": observation.on_hold_seconds, "volume_unlimited": observation.volume_unlimited,
            "time_unlimited": observation.time_unlimited, "synced_at": now,
            "panel_available": True, "panel_checked_at": now}


async def read_integration_service(session: AsyncSession, access: ReadAccess, customer_id: str,
                                    service_id: str, *, refresh: bool = True) -> ServiceView:
    authority = await read_authority(session, access, frozenset({"services:read"}))
    customer = await mapped_customer(session, authority, customer_id)
    row = (await session.execute(scoped_services(authority, customer.id).where(UserService.public_id == service_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    service, snapshot, plan_id = row
    local = service_view(*row, customer_id)
    if not refresh or not service.pg_user_id or snapshot is None or snapshot.state != "ready":
        return local
    internal_customer, internal_service, version = customer.id, service.id, snapshot.resource_version
    identity = PanelIdentity(service.pg_username, authority.owner.context.pg_admin_username or "", service.pg_user_id)
    client = await integration_pg_client(session, authority)
    await session.rollback()
    source = "local"
    values = {"panel_available": False, "panel_checked_at": datetime.now(timezone.utc)}
    try:
        async with _refresh_limit(authority.integration_id):
            with web_read_budget(seconds=3):
                payload = await client.get_user_by_id(identity.panel_id)
        observation = observe_service(payload, identity)
        values = observation_values(observation, datetime.now(timezone.utc))
        source = "panel"
    except IntegrationError:
        values.update(state="review", panel_available=True)
    except (httpx.HTTPError, PasarGuardError) as exc:
        # Upstream exceptions may contain credentials; retain only the failure class.
        logger.warning("Integration observation unavailable service=%s type=%s", internal_service, type(exc).__name__)
    await verify_credential(session, access.bearer, lock_integration=True)
    authority = await read_authority(session, access, frozenset({"services:read"}))
    scoped_ids = scoped_services(authority, internal_customer).with_only_columns(UserService.id)
    claimed = await session.execute(update(IntegrationServiceSnapshot).where(
        IntegrationServiceSnapshot.service_id == internal_service,
        IntegrationServiceSnapshot.service_id.in_(scoped_ids), IntegrationServiceSnapshot.resource_version == version,
        IntegrationServiceSnapshot.state == "ready",
        ~select(ServiceMutationGuard.service_id).where(ServiceMutationGuard.service_id == internal_service).exists(),
    ).values(**values, resource_version=IntegrationServiceSnapshot.resource_version + 1)
        .execution_options(synchronize_session=False))
    if claimed.rowcount and source == "panel":
        await session.execute(update(UserService).where(UserService.id == internal_service, UserService.id.in_(scoped_ids)).values(
            quota_data_limit_bytes=observation.limit_bytes or 0, quota_expire_at=observation.expires_at,
            quota_status=observation.status, quota_synced_at=values["synced_at"],
        ).execution_options(synchronize_session=False))
    if claimed.rowcount:
        await service_event(session, internal_service)
    await session.commit()
    row = (await session.execute(scoped_services(authority, internal_customer).where(UserService.public_id == service_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    return service_view(*row, customer_id, source=source if claimed.rowcount else "local")
