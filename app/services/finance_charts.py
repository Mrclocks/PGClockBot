"""Daily charts for the finance reports tab: orders and new users per Tehran day.

Drawn by the dashboard's sales chart (``sales-chart.js``) in the same style, each with a
7 / 30 day switch and the change against the period before. One pass of two aggregate
queries (conditional sums per day, like ``dashboard_trend``) covers 60 days, scoped to one
shop; both ranges are cut from it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TypedDict

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order
from app.services.dashboard_trend import _day_starts, _labels, change_percent
from app.services.db_safe import rollback_quiet
from app.services.demo_users import non_demo_customer
from app.services.shop_membership import shop_member_clause

logger = logging.getLogger(__name__)

CHART_RANGES = (7, 30)
LONGEST = max(CHART_RANGES)
# Method shares beyond this many are grouped, so the share bar stays readable.
MAX_METHOD_SEGMENTS = 5


class SeriesDay(TypedDict):
    date: str
    weekday: str
    value: int
    extra: int


class Series(TypedDict):
    """One chart range in the shape ``sales-chart.js`` reads (metric ``value``)."""

    days: list[SeriesDay]
    total: int
    previous_total: int
    change_pct: int | None
    has_data: bool


class FinanceCharts(TypedDict):
    orders: dict[int, Series]
    new_users: dict[int, Series]


class MethodSegment(TypedDict):
    label: str
    amount: int
    share_pct: int
    tone: int


def _per_day(column, starts: list[datetime], expr) -> list:
    return [
        func.coalesce(func.sum(case(((column >= starts[i]) & (column < starts[i + 1]), expr), else_=0)), 0)
        for i in range(len(starts) - 1)
    ]


async def _daily_counts(
    session: AsyncSession, *, reseller_id: int | None, starts: list[datetime]
) -> tuple[list[int], list[int], list[int]]:
    """Orders, delivered orders and new customers per day, oldest first."""
    scope = Order.reseller_id.is_(None) if reseller_id is None else Order.reseller_id == int(reseller_id)
    delivered = case((Order.status == "delivered", 1), else_=0)
    orders_row = (
        await session.execute(
            select(*_per_day(Order.created_at, starts, 1), *_per_day(Order.created_at, starts, delivered))
            .select_from(Order)
            .where(Order.created_at >= starts[0], Order.created_at < starts[-1], scope, non_demo_customer(Order.user_id))
        )
    ).one()
    users_row = (
        await session.execute(
            select(*_per_day(BotUser.created_at, starts, 1))
            .select_from(BotUser)
            .where(
                BotUser.created_at >= starts[0],
                BotUser.created_at < starts[-1],
                shop_member_clause(reseller_id),
                BotUser.is_demo.is_(False),
            )
        )
    ).one()
    days = len(starts) - 1
    values = [int(v or 0) for v in orders_row]
    return values[:days], values[days:], [int(v or 0) for v in users_row]


def _series(values: list[int], extras: list[int], days: int, labels: list[tuple[str, str]]) -> Series:
    """The last ``days`` of ``values`` (oldest first), compared with the ``days`` before them."""
    current, previous = values[-days:], values[-2 * days:-days]
    extra = extras[-days:]
    total, previous_total = sum(current), sum(previous)
    return {
        "days": [
            {"date": date, "weekday": weekday, "value": value, "extra": more}
            for (date, weekday), value, more in zip(labels[-days:], current, extra, strict=True)
        ],
        "total": total,
        "previous_total": previous_total,
        "change_pct": change_percent(total, previous_total),
        "has_data": total > 0,
    }


def _build(orders: list[int], delivered: list[int], users: list[int], labels: list[tuple[str, str]]) -> FinanceCharts:
    return {
        "orders": {days: _series(orders, delivered, days, labels) for days in CHART_RANGES},
        "new_users": {days: _series(users, [0] * len(users), days, labels) for days in CHART_RANGES},
    }


async def finance_charts(session: AsyncSession, *, reseller_id: int | None, now: datetime | None = None) -> FinanceCharts:
    """Orders (with delivered) and new users for 7 and 30 days; empty charts on a DB error."""
    starts = _day_starts(LONGEST * 2, now=now)
    labels = _labels(LONGEST, now=now)
    try:
        orders, delivered, users = await _daily_counts(session, reseller_id=reseller_id, starts=starts)
    except SQLAlchemyError:
        logger.warning("finance charts failed reseller_id=%s", reseller_id, exc_info=True)
        await rollback_quiet(session)
        zeros = [0] * (LONGEST * 2)
        return _build(zeros, zeros, zeros, labels)
    return _build(orders, delivered, users, labels)


def method_segments(methods: list[dict]) -> list[MethodSegment]:
    """The payment-method rows as share-bar segments; the smallest are grouped as «سایر»."""
    rows = [m for m in methods if int(m.get("amount") or 0) > 0]
    head, rest = rows[: MAX_METHOD_SEGMENTS - 1], rows[MAX_METHOD_SEGMENTS - 1:]
    if len(rest) == 1:
        head, rest = rows, []
    segments: list[MethodSegment] = [
        {"label": str(m["label"]), "amount": int(m["amount"]), "share_pct": int(m.get("share_pct") or 0), "tone": i + 1}
        for i, m in enumerate(head)
    ]
    if rest:
        segments.append({
            "label": "سایر", "amount": sum(int(m["amount"]) for m in rest),
            "share_pct": sum(int(m.get("share_pct") or 0) for m in rest), "tone": len(segments) + 1,
        })
    return segments
