"""Bounded private monitoring views computed entirely from saved evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget
from app.services.connection_monitor_access import MonitorAccess, require_monitor_owner
from app.services.connection_monitor_settings import LOCAL_PROBE_ID, MonitorSettings, monitor_settings
from app.services.connection_monitor_snapshots import snapshot_health, stored_utc
from app.services.connection_monitor_types import MAX_MONITOR_ENDPOINTS, MonitorIssue
from app.services.connection_status_policy import aggregate_host, is_fresh
from app.services.connection_status_types import (
    SLOW_DELAY_MS,
    ConnectionStatus,
    EndpointIdentity,
    HostHealth,
    MonitorReason,
)


PAGE_SIZE = 10
SAFE_REASON_CODES = frozenset(item.value for item in (*MonitorIssue, *MonitorReason))


@dataclass(frozen=True, slots=True)
class MonitorTargetView:
    id: int
    config_key: str | None
    config_name: str
    public_name: str
    display_order: int
    test_enabled: bool
    is_public: bool
    manual_pending: bool
    health: HostHealth
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MonitorAdminPage:
    settings: MonitorSettings
    targets: tuple[MonitorTargetView, ...]
    next_id: int | None
    stop_reason: str | None = None


def target_health(target: ConnectionMonitorTarget, snapshots: tuple[ConnectionMonitorSnapshot, ...], *,
                  runtime_ready: bool, now: datetime, runtime_enabled: bool = True) -> HostHealth:
    checked_at = last_target_check(target, snapshots)
    if not target.test_enabled or not runtime_enabled:
        return HostHealth(ConnectionStatus.PAUSED, checked_at, None, 0, target.endpoint_count)
    if not target.generation or not target.endpoint_count:
        return HostHealth(ConnectionStatus.NOT_READY, None, None, 0, target.endpoint_count)
    if not runtime_ready:
        return HostHealth(ConnectionStatus.UNKNOWN, checked_at, None, 0, target.endpoint_count)
    current = tuple(row for row in snapshots if row.generation == target.generation)
    if len(current) != target.endpoint_count:
        return HostHealth(ConnectionStatus.UNKNOWN, None, None, len(current), target.endpoint_count)
    states = tuple(snapshot_health(row) for row in current)
    expected = tuple(EndpointIdentity(row.endpoint_key, target.generation) for row in current)
    return replace(aggregate_host(states, expected=expected, now=now), checked_at=checked_at)


def last_target_check(target: ConnectionMonitorTarget, snapshots: tuple[ConnectionMonitorSnapshot, ...]) -> datetime | None:
    times = [stored_utc(row.checked_at) for row in snapshots if row.generation == target.generation and row.checked_at is not None]
    return min(times) if target.endpoint_count and len(times) == target.endpoint_count else None


def target_reason(target: ConnectionMonitorTarget, snapshots: tuple[ConnectionMonitorSnapshot, ...], *,
                  health: HostHealth, runtime_ready: bool, now: datetime) -> str | None:
    if health.status == ConnectionStatus.PAUSED:
        return MonitorReason.PAUSED
    if target.generation and not runtime_ready:
        return MonitorReason.CATALOG_UNAVAILABLE
    if health.checked_at is not None and not is_fresh(health.checked_at, now=now):
        return MonitorReason.STALE
    current = tuple(row for row in snapshots if row.generation == target.generation)
    reasons = [row.reason for row in current if row.status != ConnectionStatus.HEALTHY and row.reason in SAFE_REASON_CODES]
    reasons.extend(row.reason for row in current if row.reason in SAFE_REASON_CODES)
    if reasons:
        return reasons[0]
    if health.status == ConnectionStatus.DEGRADED:
        return MonitorReason.SLOW if health.delay_ms is not None and health.delay_ms > SLOW_DELAY_MS else MonitorReason.RECOVERING
    if health.status == ConnectionStatus.UNKNOWN and health.checked_at is None:
        return MonitorReason.NO_OBSERVATION
    return None


async def target_views(session: AsyncSession, targets: list[ConnectionMonitorTarget], *,
                       runtime_ready: bool, now: datetime, runtime_enabled: bool = True) -> tuple[MonitorTargetView, ...]:
    if len(targets) > PAGE_SIZE:
        raise ValueError("Invalid monitoring page")
    rows = (await session.scalars(select(ConnectionMonitorSnapshot).where(
        ConnectionMonitorSnapshot.reseller_id.is_(None), ConnectionMonitorSnapshot.target_id.in_([target.id for target in targets]),
        ConnectionMonitorSnapshot.probe_id == LOCAL_PROBE_ID,
    ).order_by(ConnectionMonitorSnapshot.target_id, ConnectionMonitorSnapshot.id).limit(PAGE_SIZE * (MAX_MONITOR_ENDPOINTS + 1)))).all()
    by_target: dict[int, list[ConnectionMonitorSnapshot]] = {}
    for row in rows:
        by_target.setdefault(row.target_id, []).append(row)
    views = []
    for target in targets:
        snapshots = tuple(by_target.get(target.id, ()))
        health = target_health(target, snapshots, runtime_ready=runtime_ready, now=now, runtime_enabled=runtime_enabled)
        reason = target_reason(target, snapshots, health=health, runtime_ready=runtime_ready, now=now)
        views.append(MonitorTargetView(target.id, target.subscription_key, target.config_name, target.public_name, target.display_order,
                                       target.test_enabled, target.is_public, target.manual_pending, health, reason))
    return tuple(views)


async def monitor_admin_page(session: AsyncSession, access: MonitorAccess, *, now: datetime, after_id: int = 0) -> MonitorAdminPage:
    if type(after_id) is not int or not 0 <= after_id <= 2147483647:
        raise ValueError("صفحه نامعتبر است.")
    settings = await monitor_settings(session, access)
    targets = (await session.scalars(select(ConnectionMonitorTarget).where(
        ConnectionMonitorTarget.reseller_id.is_(None), ConnectionMonitorTarget.id > after_id,
    ).order_by(ConnectionMonitorTarget.id).limit(PAGE_SIZE + 1))).all()
    state = await session.scalar(select(ConnectionMonitorState).where(
        ConnectionMonitorState.id == settings.id, ConnectionMonitorState.reseller_id.is_(None),
    ).limit(1))
    ready = state is not None and settings.enabled and is_fresh(stored_utc(state.catalog_synced_at), now=now)
    views = await target_views(session, targets[:PAGE_SIZE], runtime_ready=ready, now=now, runtime_enabled=settings.enabled)
    stop_reason = None
    if not settings.enabled:
        stop_reason = await session.scalar(select(ConnectionMonitorSnapshot.reason).where(
            ConnectionMonitorSnapshot.reseller_id.is_(None),
            ConnectionMonitorSnapshot.reason == MonitorReason.USER_MISSING.value,
        ).limit(1))
    await session.commit()
    return MonitorAdminPage(
        settings, views, views[-1].id if len(targets) > PAGE_SIZE else None, stop_reason,
    )


async def monitor_target_view(session: AsyncSession, access: MonitorAccess, *, target_id: int, now: datetime) -> MonitorTargetView:
    if type(target_id) is not int or not 1 <= target_id <= 2147483647:
        raise ValueError("هاست تست یافت نشد.")
    await require_monitor_owner(session, access)
    target = await session.scalar(select(ConnectionMonitorTarget).where(
        ConnectionMonitorTarget.id == target_id, ConnectionMonitorTarget.reseller_id.is_(None),
    ).limit(1))
    if target is None:
        raise ValueError("هاست تست یافت نشد.")
    state = await session.scalar(select(ConnectionMonitorState).where(
        ConnectionMonitorState.reseller_id.is_(None), ConnectionMonitorState.probe_id == LOCAL_PROBE_ID,
    ).limit(1))
    ready = state is not None and state.enabled and is_fresh(stored_utc(state.catalog_synced_at), now=now)
    view = (await target_views(session, [target], runtime_ready=ready, now=now, runtime_enabled=state is not None and state.enabled))[0]
    await session.commit()
    return view
