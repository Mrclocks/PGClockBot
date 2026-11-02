"""Daily sales trend and the signed-in staff's own account summary for the dashboard.

The trend is one aggregate query (conditional sums per Tehran day). The 30-day range is
cached for a short time per shop so switching the chart back and forth does not repeat it;
the 7-day range is computed with the rest of the dashboard so its figures always match the
«فروش دوره‌ای» card on the same page.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, UserService
from app.services.db_safe import rollback_quiet
from app.services.demo_users import non_demo_customer
from app.services.service_scope import shop_services_clause

logger = logging.getLogger(__name__)

TEHRAN = ZoneInfo("Asia/Tehran")
ALLOWED_DAYS = (7, 30)
CACHED_DAYS = frozenset({30})
CACHE_SECONDS = 60.0
CACHE_MAX_ENTRIES = 64
# Panel statuses of a service that cannot be used right now.
INACTIVE_QUOTA_STATUSES = ("expired", "disabled", "limited")
# Python weekday() is Monday=0.
WEEKDAYS_FA = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")


class TrendDay(TypedDict):
    date: str
    weekday: str
    revenue: int
    orders: int


class SalesTrend(TypedDict):
    days: list[TrendDay]
    total: int
    previous_total: int
    change_pct: int | None
    today: int
    yesterday: int
    today_change_pct: int | None
    has_data: bool


_cache: dict[tuple[int | None, int], tuple[float, SalesTrend]] = {}


def change_percent(current: int, previous: int) -> int | None:
    """Whole-number change versus the previous period; ``None`` when there is nothing to compare with."""
    if previous <= 0:
        return None
    return round((current - previous) * 100 / previous)


def _day_starts(count: int, *, now: datetime | None = None) -> list[datetime]:
    """UTC instants of Tehran midnight for the last ``count`` days, oldest first, plus the end of today."""
    local = (now or datetime.now(TEHRAN)).astimezone(TEHRAN)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [today - timedelta(days=offset) for offset in range(count - 1, -1, -1)]
    starts.append(today + timedelta(days=1))
    return [moment.astimezone(timezone.utc) for moment in starts]


def empty_trend(days: int = 7) -> SalesTrend:
    labels = _labels(days)
    return {
        "days": [{"date": d, "weekday": w, "revenue": 0, "orders": 0} for d, w in labels],
        "total": 0, "previous_total": 0, "change_pct": None,
        "today": 0, "yesterday": 0, "today_change_pct": None, "has_data": False,
    }


def _labels(count: int, *, now: datetime | None = None) -> list[tuple[str, str]]:
    local = (now or datetime.now(TEHRAN)).astimezone(TEHRAN).date()
    out = []
    for offset in range(count - 1, -1, -1):
        day = local - timedelta(days=offset)
        out.append((day.isoformat(), WEEKDAYS_FA[day.weekday()]))
    return out


async def sales_trend(
    session: AsyncSession, *, reseller_id: int | None, days: int = 7, now: datetime | None = None
) -> SalesTrend:
    """Delivered revenue and order count per Tehran day for the last ``days`` days, plus the previous period."""
    if days not in ALLOWED_DAYS:
        raise ValueError("بازهٔ نمودار معتبر نیست")
    key = (int(reseller_id) if reseller_id else None, days)
    use_cache = now is None and days in CACHED_DAYS
    cached = _cache.get(key)
    if use_cache and cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]

    starts = _day_starts(days * 2, now=now)
    delivered = Order.status == "delivered"
    scope = Order.reseller_id.is_(None) if key[0] is None else Order.reseller_id == key[0]
    columns = []
    for index in range(days * 2):
        in_day = (Order.created_at >= starts[index]) & (Order.created_at < starts[index + 1])
        columns.append(func.coalesce(func.sum(case((in_day & delivered, Order.amount), else_=0)), 0))
        columns.append(func.coalesce(func.sum(case((in_day & delivered, 1), else_=0)), 0))
    row = (
        await session.execute(
            select(*columns).select_from(Order).where(
                Order.created_at >= starts[0], Order.created_at < starts[-1], scope, non_demo_customer(Order.user_id)
            )
        )
    ).one()
    values = [int(value or 0) for value in row]
    revenue = values[0::2]
    orders = values[1::2]
    labels = _labels(days, now=now)
    current = [
        {"date": labels[i][0], "weekday": labels[i][1], "revenue": revenue[days + i], "orders": orders[days + i]}
        for i in range(days)
    ]
    total = sum(revenue[days:])
    previous_total = sum(revenue[:days])
    today, yesterday = revenue[-1], revenue[-2]
    result: SalesTrend = {
        "days": current, "total": total, "previous_total": previous_total,
        "change_pct": change_percent(total, previous_total),
        "today": today, "yesterday": yesterday, "today_change_pct": change_percent(today, yesterday),
        "has_data": total > 0,
    }
    if use_cache:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.clear()
        _cache[key] = (time.monotonic(), result)
    return result


async def sales_trend_or_empty(session: AsyncSession, *, reseller_id: int | None, days: int = 7) -> SalesTrend:
    """The trend, or an empty chart when the query fails; the page around it still renders."""
    try:
        return await sales_trend(session, reseller_id=reseller_id, days=days)
    except SQLAlchemyError:
        logger.warning("sales trend failed reseller_id=%s", reseller_id, exc_info=True)
        await rollback_quiet(session)
        return empty_trend(days)


def clear_trend_cache() -> None:
    _cache.clear()


async def own_account_summary(session: AsyncSession, bot_user_id: int, *, now: datetime | None = None) -> dict[str, int | None]:
    """Wallet, active platform services and nearest expiry of one account, for the «حساب من» card.

    A service counts as active when it is not cancelled, has not passed its expiry date
    (no date means no expiry) and the panel did not report it expired, disabled or limited.
    """
    moment = now or datetime.now(timezone.utc)
    wallet = await session.scalar(select(BotUser.wallet_balance).where(BotUser.id == int(bot_user_id)))
    row = (
        await session.execute(
            select(func.count(), func.min(UserService.quota_expire_at))
            .select_from(UserService)
            .where(
                UserService.bot_user_id == int(bot_user_id),
                UserService.is_cancelled.is_(False),
                shop_services_clause(None),
                or_(UserService.quota_expire_at.is_(None), UserService.quota_expire_at >= moment),
                or_(UserService.quota_status.is_(None), UserService.quota_status.not_in(INACTIVE_QUOTA_STATUSES)),
            )
        )
    ).one()
    nearest = row[1]
    if nearest is not None and nearest.tzinfo is None:
        nearest = nearest.replace(tzinfo=timezone.utc)
    days_left = None if nearest is None else max(0, (nearest - moment).days)
    return {"wallet": int(wallet or 0), "services": int(row[0] or 0), "nearest_expiry_days": days_left}
