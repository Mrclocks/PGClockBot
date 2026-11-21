"""Read published connection evidence only from the authenticated bot shop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget, Setting
from app.services.connection_monitor_claims import eligible_monitor_account
from app.services.connection_monitor_settings import LOCAL_PROBE_ID
from app.services.connection_monitor_snapshots import stored_utc
from app.services.connection_monitor_timing import INTERVAL_SETTING_KEY, parse_interval, timing_for
from app.services.connection_monitor_types import MAX_MONITOR_ENDPOINTS
from app.services.connection_monitor_views import PAGE_SIZE, target_health
from app.services.connection_status_policy import is_fresh
from app.services.connection_status_types import FRESHNESS, ConnectionStatus, require_aware
from app.services.users import current_shop_reseller_id


PROBE_LABEL = "بررسی از سرور ربات"
INVALID_PAGE = "فهرست تغییر کرده است؛ بروزرسانی کنید."


@dataclass(frozen=True, slots=True)
class PublicConnectionStatus:
    public_name: str
    status: ConnectionStatus
    checked_at: datetime | None
    probe_label: str = PROBE_LABEL
    is_catalog_stale: bool = False


@dataclass(frozen=True, slots=True)
class PublicConnectionPage:
    entries: tuple[PublicConnectionStatus, ...]
    next_id: int | None = None
    # How long a check stays current at the configured interval; renderers mark older results.
    freshness: timedelta = FRESHNESS


def _scope(column: InstrumentedAttribute[int | None], shop_id: int | None) -> ColumnElement[bool]:
    return column.is_(None) if shop_id is None else column == shop_id


def _published(shop_id: int | None) -> tuple[ColumnElement[bool], ...]:
    return (_scope(ConnectionMonitorTarget.reseller_id, shop_id), ConnectionMonitorTarget.test_enabled.is_(True),
            ConnectionMonitorTarget.is_public.is_(True), func.length(func.trim(ConnectionMonitorTarget.public_name)) > 0,
            func.length(ConnectionMonitorTarget.public_name) <= 64,
            ConnectionMonitorTarget.generation != "", ConnectionMonitorTarget.endpoint_count > 0)


def _public_entry(target: ConnectionMonitorTarget, rows: tuple[ConnectionMonitorSnapshot, ...], *,
                   ready: bool, now: datetime, freshness: timedelta) -> PublicConnectionStatus:
    health = target_health(target, rows, runtime_ready=ready, now=now, freshness=freshness)
    status = health.status if health.status in (ConnectionStatus.HEALTHY, ConnectionStatus.DEGRADED, ConnectionStatus.DOWN) else ConnectionStatus.UNKNOWN
    return PublicConnectionStatus(target.public_name, status, health.checked_at, is_catalog_stale=not ready)


async def public_connection_page(session: AsyncSession, *, now: datetime, after_id: int = 0) -> PublicConnectionPage:
    require_aware(now)
    shop_id = current_shop_reseller_id()
    if shop_id is not None and (type(shop_id) is not int or not 1 <= shop_id <= 2147483647):
        raise ValueError(INVALID_PAGE)
    if type(after_id) is not int or not 0 <= after_id <= 2147483647:
        raise ValueError(INVALID_PAGE)
    # The interval setting rides on the same query: a public read stays at three selects.
    interval = select(Setting.value).where(Setting.key == INTERVAL_SETTING_KEY).limit(1).scalar_subquery()
    runtime = (await session.execute(select(ConnectionMonitorState.catalog_synced_at, interval.label("interval")).where(
        _scope(ConnectionMonitorState.reseller_id, shop_id), ConnectionMonitorState.probe_id == LOCAL_PROBE_ID,
        ConnectionMonitorState.enabled.is_(True), ConnectionMonitorState.pg_user_id.is_not(None),
        ConnectionMonitorState.subscription_token_enc.is_not(None), ConnectionMonitorState.account_revision != "",
        eligible_monitor_account(),
    ).limit(1))).first()
    if runtime is None:
        await session.commit()
        return PublicConnectionPage(())
    conditions = list(_published(shop_id))
    if after_id:
        cursor = (await session.execute(select(ConnectionMonitorTarget.display_order, ConnectionMonitorTarget.id).where(
            *_published(shop_id), ConnectionMonitorTarget.id == after_id,
        ).limit(1))).first()
        if cursor is None:
            raise ValueError(INVALID_PAGE)
        conditions.append(or_(ConnectionMonitorTarget.display_order > cursor.display_order,
                              (ConnectionMonitorTarget.display_order == cursor.display_order) & (ConnectionMonitorTarget.id > cursor.id)))
    targets = (await session.scalars(select(ConnectionMonitorTarget).where(*conditions).order_by(
        ConnectionMonitorTarget.display_order, ConnectionMonitorTarget.id,
    ).limit(PAGE_SIZE + 1))).all()
    visible = targets[:PAGE_SIZE]
    rows = (await session.scalars(select(ConnectionMonitorSnapshot).where(
        _scope(ConnectionMonitorSnapshot.reseller_id, shop_id), ConnectionMonitorSnapshot.target_id.in_([target.id for target in visible]),
        ConnectionMonitorSnapshot.probe_id == LOCAL_PROBE_ID,
    ).order_by(ConnectionMonitorSnapshot.id).limit(PAGE_SIZE * (MAX_MONITOR_ENDPOINTS + 1)))).all()
    by_target: dict[int, list[ConnectionMonitorSnapshot]] = {}
    for row in rows:
        by_target.setdefault(row.target_id, []).append(row)
    timing = timing_for(parse_interval(runtime.interval))
    ready = is_fresh(stored_utc(runtime.catalog_synced_at), now=now, freshness=timing.freshness)
    entries = tuple(
        _public_entry(target, tuple(by_target.get(target.id, ())), ready=ready, now=now, freshness=timing.freshness)
        for target in visible
    )
    await session.commit()
    return PublicConnectionPage(entries, visible[-1].id if len(targets) > PAGE_SIZE else None, timing.freshness)
