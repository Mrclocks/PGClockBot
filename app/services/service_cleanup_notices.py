"""Warn customers before their expired or volume-finished service is deleted.

Per scope (a shop, or the platform) and rule (time, volume) a service goes through up to three
stages: a notice when it expired (with the deadline), a first and a last warning before the
deadline. A stage is claimed with a conditional UPDATE and its message is queued in the same
transaction, so a repeated run sends nothing twice; a renewal resets the stage (see
``sync_service_quota_cache``). The panel is read once more before a stage is sent, so a service that
was renewed in the panel is not warned.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, UserService
from app.services.bot_user_admin import panel_client_for_service, sync_service_quota_cache
from app.services.customer_notice_queue import add_notice
from app.services.service_cleanup import (
    KIND_TIME,
    KIND_VOLUME,
    STAGE_NONE,
    CleanupPolicy,
    deadline_for,
    eligible_clauses,
    local_date,
    shop_services_clause,
    target_stage,
    time_due_clause,
    volume_due_clause,
)

logger = logging.getLogger(__name__)

SCAN_BATCH = 100
# A service already in a warning stage is read again at most this often, to notice a renewal made in the panel.
RECHECK_AFTER = timedelta(hours=12)
LIVE_CONCURRENCY = 4
LIVE_TIMEOUT_SECONDS = 12
NOTICE_ACTIONS = {
    (KIND_TIME, 1): "cleanup_expired",
    (KIND_TIME, 2): "cleanup_warning",
    (KIND_TIME, 3): "cleanup_warning",
    (KIND_VOLUME, 1): "cleanup_volume_finished",
    (KIND_VOLUME, 2): "cleanup_volume_warning",
    (KIND_VOLUME, 3): "cleanup_volume_warning",
}


@dataclass(frozen=True, slots=True)
class ScanResult:
    queued: int = 0
    reset: int = 0


def stage_column(kind: str):
    return UserService.cleanup_warn_stage if kind == KIND_TIME else UserService.cleanup_volume_warn_stage


def stage_of(service: UserService, kind: str) -> int:
    return int((service.cleanup_warn_stage if kind == KIND_TIME else service.cleanup_volume_warn_stage) or 0)


def is_over(service: UserService, kind: str, now: datetime) -> bool:
    """True while the service is still expired / out of volume according to its (refreshed) cache."""
    if kind == KIND_TIME:
        expire = service.quota_expire_at
        return expire is not None and (expire if expire.tzinfo else expire.replace(tzinfo=timezone.utc)) < now
    return service.volume_exhausted_at is not None


def _candidate_clause(kind: str, policy: CleanupPolicy, now: datetime):
    """SQL prefilter: services that may need a stage sent; Python then decides exactly."""
    column = stage_column(kind)
    make = time_due_clause if kind == KIND_TIME else volume_due_clause
    started = (
        and_(UserService.quota_expire_at.is_not(None), UserService.quota_expire_at < now)
        if kind == KIND_TIME
        else UserService.volume_exhausted_at.is_not(None)
    )
    options = [
        and_(started, column == STAGE_NONE),
        and_(column > STAGE_NONE, or_(UserService.quota_synced_at.is_(None), UserService.quota_synced_at < now - RECHECK_AFTER)),
    ]
    for stage, days in ((2, policy.warn_first), (3, policy.warn_last)):
        if days > 0:
            ahead = make(policy, now, ahead=timedelta(days=days))
            if ahead is not None:
                # A hold moves the deadline: the service is a candidate only once the hold ends within the window.
                hold_near = or_(UserService.cleanup_hold_until.is_(None), UserService.cleanup_hold_until <= now + timedelta(days=days))
                options.append(and_(ahead, hold_near, column < stage))
    return or_(*options)


async def live_refresh(session: AsyncSession, services: list[UserService]) -> set[int]:
    """Re-read each service from its own shop's panel and refresh its quota cache; returns the ids that were read.

    A service that could not be read is left alone: nothing about it is decided from stale data.
    """
    clients = [await panel_client_for_service(session, service) for service in services]
    gate = asyncio.Semaphore(LIVE_CONCURRENCY)

    async def read(pg: Any, service: UserService) -> dict | None:
        if pg is None or not service.pg_user_id:
            return None
        async with gate:
            try:
                info = await asyncio.wait_for(pg.get_user_by_id(int(service.pg_user_id)), timeout=LIVE_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - an unreadable service is simply skipped this run
                logger.debug("cleanup live read failed service=%s", service.id, exc_info=True)
                return None
        return info if isinstance(info, dict) else None

    infos = await asyncio.gather(*(read(pg, s) for pg, s in zip(clients, services, strict=True)))
    fresh: set[int] = set()
    for service, info in zip(services, infos, strict=True):
        if info is not None:
            sync_service_quota_cache(service, info=info)
            fresh.add(int(service.id))
    return fresh


async def _claim_stage(session: AsyncSession, service_id: int, kind: str, stage: int, now: datetime) -> bool:
    column = stage_column(kind)
    result = await session.execute(
        update(UserService).where(UserService.id == service_id, column < stage).values({column.key: stage, UserService.cleanup_warned_at.key: now}),
        execution_options={"synchronize_session": False},
    )
    return result.rowcount == 1


async def scan_notices(
    session: AsyncSession, *, shop_id: int | None, policy: CleanupPolicy, kind: str, now: datetime | None = None,
    limit: int = SCAN_BATCH,
) -> ScanResult:
    """Queue the due warnings of one scope and rule; one bounded batch per call."""
    now = now or datetime.now(timezone.utc)
    days = policy.time_days if kind == KIND_TIME else policy.volume_days
    if not (policy.time_on if kind == KIND_TIME else policy.volume_on):
        return ScanResult()
    rows = (
        await session.execute(
            select(UserService, BotUser.telegram_id)
            .join(BotUser, BotUser.id == UserService.bot_user_id)
            # A service under a hold is scanned too: its deadline is the hold's end, so its warnings come before it.
            .where(shop_services_clause(shop_id), *eligible_clauses(), BotUser.is_blocked.is_(False),
                   _candidate_clause(kind, policy, now))
            .order_by(UserService.id)
            .limit(limit)
        )
    ).all()
    services = [service for service, _tg in rows]
    telegram = {int(service.id): int(tg or 0) for service, tg in rows}
    if not services:
        return ScanResult()
    fresh = await live_refresh(session, services)
    queued = reset = 0
    for service in services:
        stage = stage_of(service, kind)
        if int(service.id) not in fresh:
            continue
        if not is_over(service, kind, now):
            if stage:
                await session.execute(
                    update(UserService).where(UserService.id == service.id).values({stage_column(kind).key: 0}),
                    execution_options={"synchronize_session": False},
                )
                reset += 1
            continue
        deadline = deadline_for(service, policy, kind)
        if deadline is None or not telegram[int(service.id)]:
            continue
        stage_now = target_stage(deadline, now=now, policy=policy, days=days)
        if stage_now <= stage or not await _claim_stage(session, int(service.id), kind, stage_now, now):
            continue
        add_notice(
            session, shop_id=service.shop_id, bot_user_id=int(service.bot_user_id), telegram_id=telegram[int(service.id)],
            action=NOTICE_ACTIONS[(kind, stage_now)], service_name=service.pg_username or "",
            reason=local_date(deadline), service_id=int(service.id),
        )
        queued += 1
    await session.commit()
    return ScanResult(queued=queued, reset=reset)
