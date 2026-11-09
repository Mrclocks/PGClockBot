"""Re-arm low-time / low-volume alerts that were already sent but no longer apply.

Renewals, extensions and add-on packs re-arm through the quota cache (``sync_service_quota_cache``).
What that cannot see is a usage reset or a change made directly in the panel, so a bounded sweep
re-reads the services whose alert flag is set, least recently checked first, and clears the flag
once the remaining share is clearly above the shop's threshold.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserService
from app.services.formatting import parse_expire
from app.services.usage_alerts import (
    period_seconds,
    plan_durations,
    should_rearm,
    time_remaining_pct,
    traffic_remaining_pct,
)

logger = logging.getLogger(__name__)

SWEEP_LIMIT = 100
FETCH_TIMEOUT_SECONDS = 12
FETCH_CONCURRENCY = 8

Thresholds = tuple[bool, int, int]  # (alerts on, traffic %, time %)
ThresholdsFor = Callable[[int | None], Awaitable[Thresholds]]


async def _fetch(pg: Any, gate: asyncio.Semaphore, token: str) -> dict | None:
    async with gate:
        try:
            info = await asyncio.wait_for(pg.subscription_info(token), timeout=FETCH_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - one unreadable service must not stop the sweep
            logger.debug("alert re-arm lookup failed", exc_info=True)
            return None
    return info if isinstance(info, dict) else None


def _rearm_one(service: UserService, info: dict, *, now: datetime, plan_days: int | None, time_pct: int, traffic_pct: int) -> bool:
    """Clear the flags of ``service`` that no longer apply; True when something changed."""
    changed = False
    expire = parse_expire(info.get("expire"))
    if service.notified_expire and expire is not None:
        period = period_seconds(plan_days=plan_days, created_at=service.created_at, expire=expire)
        if should_rearm(time_remaining_pct(expire, now=now, period=period), time_pct):
            service.notified_expire = False
            changed = True
    if service.notified_traffic and info.get("data_limit"):
        try:
            used, limit = float(info.get("used_traffic") or 0), float(info["data_limit"])
        except (TypeError, ValueError):
            return changed
        if should_rearm(traffic_remaining_pct(used, limit), traffic_pct):
            service.notified_traffic = False
            changed = True
    return changed


async def rearm_flagged_alerts(
    session: AsyncSession, pg: Any, thresholds_for: ThresholdsFor, *, now: datetime, limit: int = SWEEP_LIMIT
) -> int:
    """Re-read up to ``limit`` flagged services and clear stale flags; returns how many were re-armed."""
    from app.services.bot_user_admin import sync_service_quota_cache
    from app.services.service_live_info import validate_service_info

    services = list(
        (
            await session.scalars(
                select(UserService)
                .where(
                    UserService.subscription_token.is_not(None),
                    UserService.shop_needs_review.is_(False),
                    or_(UserService.notified_expire.is_(True), UserService.notified_traffic.is_(True)),
                )
                .order_by(UserService.quota_synced_at.asc().nulls_first(), UserService.id)
                .limit(limit)
            )
        ).all()
    )
    if not services:
        return 0
    plans = await plan_durations(session, services)
    settings = {shop: await thresholds_for(shop) for shop in {s.shop_id for s in services}}
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)
    infos = await asyncio.gather(*(_fetch(pg, gate, s.subscription_token) for s in services))
    rearmed = 0
    for service, info in zip(services, infos, strict=True):
        enabled, traffic_pct, time_pct = settings[service.shop_id]
        if info is None or not enabled:
            continue
        checked = validate_service_info(info)
        if checked.get("status") and "expire" in checked:
            sync_service_quota_cache(service, info=checked)
        if _rearm_one(
            service, info, now=now, plan_days=plans.get(int(service.plan_id)) if service.plan_id else None,
            time_pct=time_pct, traffic_pct=traffic_pct,
        ):
            rearmed += 1
    await session.commit()
    return rearmed
