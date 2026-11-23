"""Owner manual checks enqueue the existing worker path with atomic cooldowns."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConnectionMonitorState, ConnectionMonitorTarget
from app.services.connection_monitor_access import MonitorAccess, active_monitor_owner, require_monitor_owner
from app.services.connection_monitor_claims import eligible_monitor_account
from app.services.connection_monitor_settings import LOCAL_PROBE_ID
from app.services.connection_monitor_snapshots import stored_utc
from app.services.connection_monitor_timing import monitor_timing
from app.services.connection_status_types import require_aware


MANUAL_COOLDOWN = timedelta(seconds=60)
QUEUE_UNAVAILABLE = "تست هنوز آماده نیست یا درخواست قبلی در صف است. تست خودکار را روشن کنید و کمی بعد دوباره تلاش کنید."
QUEUE_RUNTIME_PENDING = "هنوز اولین بررسی کامل نشده یا اطلاعات پنل به‌روز نیست؛ کمی بعد دوباره تلاش کنید."


def _target_queue_filters(access: MonitorAccess, *, target_id: int, now: datetime) -> tuple[object, ...]:
    return (
        ConnectionMonitorTarget.id == target_id,
        ConnectionMonitorTarget.reseller_id.is_(None),
        ConnectionMonitorTarget.test_enabled.is_(True),
        ConnectionMonitorTarget.generation != "",
        ConnectionMonitorTarget.endpoint_count > 0,
        ConnectionMonitorTarget.manual_pending.is_(False),
        or_(ConnectionMonitorTarget.manual_available_at.is_(None), ConnectionMonitorTarget.manual_available_at <= now),
        or_(ConnectionMonitorTarget.claim_until.is_(None), ConnectionMonitorTarget.claim_until <= now),
        active_monitor_owner(access),
    )


async def _manual_queue_rejection(session: AsyncSession, access: MonitorAccess, *,
                                  target_id: int, now: datetime) -> str:
    """Distinguish a not-yet-fresh catalog from every other failed claim cause."""
    row = (await session.execute(select(ConnectionMonitorState.catalog_synced_at).select_from(
        ConnectionMonitorTarget,
    ).join(ConnectionMonitorState, and_(
        ConnectionMonitorState.reseller_id.is_(None), ConnectionMonitorState.probe_id == LOCAL_PROBE_ID,
        ConnectionMonitorState.enabled.is_(True), ConnectionMonitorState.pg_user_id.is_not(None),
        ConnectionMonitorState.subscription_token_enc.is_not(None), ConnectionMonitorState.account_revision != "",
        eligible_monitor_account(),
    )).where(*_target_queue_filters(access, target_id=target_id, now=now)).limit(1))).first()
    if row is None:
        return QUEUE_UNAVAILABLE
    synced_at = stored_utc(row[0])
    freshness = (await monitor_timing(session)).freshness
    if synced_at is None or not (synced_at > now - freshness and synced_at <= now):
        return QUEUE_RUNTIME_PENDING
    return QUEUE_UNAVAILABLE


async def queue_manual_check(session: AsyncSession, access: MonitorAccess, *, target_id: int, now: datetime) -> None:
    require_aware(now)
    if type(target_id) is not int or not 1 <= target_id <= 2147483647:
        raise ValueError(QUEUE_UNAVAILABLE)
    await require_monitor_owner(session, access)
    freshness = (await monitor_timing(session)).freshness
    ready = exists(select(ConnectionMonitorState.id).where(
        ConnectionMonitorState.reseller_id.is_(None), ConnectionMonitorState.probe_id == LOCAL_PROBE_ID,
        ConnectionMonitorState.enabled.is_(True), ConnectionMonitorState.catalog_synced_at > now - freshness,
        ConnectionMonitorState.catalog_synced_at <= now,
        ConnectionMonitorState.pg_user_id.is_not(None), ConnectionMonitorState.subscription_token_enc.is_not(None),
        ConnectionMonitorState.account_revision != "", eligible_monitor_account(),
    ))
    result = await session.execute(update(ConnectionMonitorTarget).where(
        *_target_queue_filters(access, target_id=target_id, now=now), ready,
    ).values(manual_pending=True, manual_available_at=now + MANUAL_COOLDOWN, next_check_at=now))
    if result.rowcount != 1:
        await session.rollback()
        raise ValueError(await _manual_queue_rejection(session, access, target_id=target_id, now=now))
    await session.commit()
