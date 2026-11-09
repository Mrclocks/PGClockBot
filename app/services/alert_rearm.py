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
from datetime import datetime, timedelta
from typing import Any

from dataclasses import dataclass
from sqlalchemy import false, or_, select
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
# A service whose quota changed this recently is looked at first: its change is the likeliest reason to re-arm.
RECENT_CHANGE_WINDOW = timedelta(hours=12)
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


@dataclass(frozen=True, slots=True)
class SweepResult:
    rearmed: int
    next_after_id: int  # where the next sweep starts; 0 after the last flagged service


def _in_shops(enabled: set[int | None]):
    """SQL: the service belongs to one of the shops (``None`` = the platform) whose alerts are on."""
    shops = [s for s in enabled if s is not None]
    parts = [UserService.shop_id.in_(shops)] if shops else []
    if None in enabled:
        parts.append(UserService.shop_id.is_(None))
    return or_(*parts) if parts else false()


async def _enabled_shops(session: AsyncSession, thresholds_for: ThresholdsFor) -> tuple[set[int | None], dict[int | None, Thresholds]]:
    """Shops that have a flagged service and alerts switched on, with their thresholds."""
    shops = (
        await session.scalars(
            select(UserService.shop_id)
            .where(or_(UserService.notified_expire.is_(True), UserService.notified_traffic.is_(True)))
            .distinct()
        )
    ).all()
    settings = {shop: await thresholds_for(shop) for shop in shops}
    return {shop for shop, (enabled, _t, _m) in settings.items() if enabled}, settings


async def _pick(session: AsyncSession, enabled: set[int | None], *, now: datetime, after_id: int, limit: int) -> tuple[list[UserService], int]:
    """Recently changed flagged services first, then a window that moves through all of them by id.

    The window advances whether or not a service could be read, so rows the panel keeps failing on
    (a deleted user, a timeout) can never hold up the others.
    """
    base = (
        UserService.subscription_token.is_not(None),
        UserService.shop_needs_review.is_(False),
        or_(UserService.notified_expire.is_(True), UserService.notified_traffic.is_(True)),
        _in_shops(enabled),
    )
    recent_limit = limit // 2
    recent = list(
        (
            await session.scalars(
                select(UserService)
                .where(*base, UserService.quota_synced_at >= now - RECENT_CHANGE_WINDOW)
                .order_by(UserService.quota_synced_at.desc(), UserService.id)
                .limit(recent_limit)
            )
        ).all()
    )
    seen = {int(s.id) for s in recent}
    window = limit - len(recent)
    rotating = list(
        (
            await session.scalars(
                select(UserService).where(*base, UserService.id > after_id).order_by(UserService.id).limit(window)
            )
        ).all()
    )
    picked = recent + [s for s in rotating if int(s.id) not in seen]
    next_after = int(rotating[-1].id) if len(rotating) == window and rotating else 0
    return picked, next_after


async def rearm_flagged_alerts(
    session: AsyncSession, pg: Any, thresholds_for: ThresholdsFor, *, now: datetime, after_id: int = 0,
    limit: int = SWEEP_LIMIT,
) -> SweepResult:
    """Re-read up to ``limit`` flagged services and clear the flags that no longer apply."""
    # Imported here: bot_user_admin imports this package's neighbours at load time (import cycle).
    from app.services.bot_user_admin import sync_service_quota_cache
    from app.services.service_live_info import validate_service_info

    enabled, settings = await _enabled_shops(session, thresholds_for)
    if not enabled:
        return SweepResult(0, 0)
    services, next_after = await _pick(session, enabled, now=now, after_id=after_id, limit=limit)
    if not services:
        return SweepResult(0, 0)
    plans = await plan_durations(session, services)
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)
    infos = await asyncio.gather(*(_fetch(pg, gate, s.subscription_token) for s in services))
    rearmed = 0
    for service, info in zip(services, infos, strict=True):
        _enabled, traffic_pct, time_pct = settings[service.shop_id]
        if info is None:
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
    return SweepResult(rearmed, next_after)
