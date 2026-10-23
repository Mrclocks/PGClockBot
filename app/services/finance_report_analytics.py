"""Read-only, shop-scoped finance summaries and bounded sales charts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import TypedDict

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.config import get_settings
from app.db.models import BotUser, Order
from app.services.demo_users import non_demo_customer
from app.services.finance_report_ranges import (
    PERIOD_LABELS, FinanceReportRange, report_datetime_label, report_intervals,
)
from app.services.finance_reports import PaymentMethodSummary, shop_ops_snapshot, shop_payment_method_breakdown
from app.services.shop_membership import shop_member_clause

PENDING_STATUSES = ("pending", "awaiting_receipt", "awaiting_approval", "paid", "delivering")
UNSUCCESSFUL_STATUSES = ("rejected", "cancelled", "failed")
CHART_WIDTH = 800
CHART_HEIGHT = 200


class FinanceMetrics(TypedDict):
    orders: int
    delivered: int
    revenue: int
    pending: int
    unsuccessful: int
    avg_order: int
    new_users: int


@dataclass(slots=True)
class FinanceTrendPoint:
    start: str
    end: str
    revenue: int
    delivered: int
    x: int = 0
    y: int = CHART_HEIGHT


class FinanceDashboard(TypedDict):
    period: str
    period_label: str
    currency: str
    window: FinanceReportRange
    start_label: str
    end_label: str
    previous_label: str
    generated_at: str
    active: FinanceMetrics
    previous: FinanceMetrics
    change_percent: int | None
    timeline: list[FinanceTrendPoint]
    interval_label: str
    chart_points: str
    chart_peak: int
    payment_methods: list[PaymentMethodSummary]
    ops: dict[str, int]


def _sum_when(condition: ColumnElement[bool], value: int | ColumnElement[int] = 1) -> ColumnElement[int]:
    return func.coalesce(func.sum(case((condition, value), else_=0)), 0)


def _metric_columns(condition: ColumnElement[bool]) -> list[ColumnElement[int]]:
    delivered = condition & (Order.status == "delivered")
    return [
        _sum_when(condition), _sum_when(delivered), _sum_when(delivered, Order.amount),
        _sum_when(condition & Order.status.in_(PENDING_STATUSES)),
        _sum_when(condition & Order.status.in_(UNSUCCESSFUL_STATUSES)),
    ]


def _metrics(values: Sequence[int | None]) -> FinanceMetrics:
    orders, delivered, revenue, pending, unsuccessful = (int(value or 0) for value in values)
    return FinanceMetrics(
        orders=orders, delivered=delivered, revenue=revenue, pending=pending,
        unsuccessful=unsuccessful, avg_order=revenue // delivered if delivered else 0, new_users=0,
    )


async def _period_metrics(
    session: AsyncSession, *, reseller_id: int | None, window: FinanceReportRange,
) -> tuple[FinanceMetrics, FinanceMetrics]:
    current = (Order.created_at >= window.start) & (Order.created_at < window.end)
    previous = (Order.created_at >= window.previous_start) & (Order.created_at < window.start)
    scope = Order.reseller_id.is_(None) if reseller_id is None else Order.reseller_id == reseller_id
    row = (await session.execute(
        select(*_metric_columns(current), *_metric_columns(previous)).where(
            scope, non_demo_customer(Order.user_id),
            Order.created_at >= window.previous_start, Order.created_at < window.end,
        )
    )).one()
    active, prior = _metrics(row[:5]), _metrics(row[5:])
    active["new_users"] = int(await session.scalar(
        select(func.count()).select_from(BotUser).where(
            shop_member_clause(reseller_id), BotUser.is_demo.is_(False),
            BotUser.created_at >= window.start, BotUser.created_at < window.end,
        )
    ) or 0)
    return active, prior


async def _sales_timeline(
    session: AsyncSession, *, reseller_id: int | None, window: FinanceReportRange,
) -> tuple[str, list[FinanceTrendPoint]]:
    interval_label, starts = report_intervals(window)
    columns = []
    for start, end in zip(starts, starts[1:], strict=False):
        in_bucket = (Order.created_at >= start) & (Order.created_at < end)
        columns.extend((_sum_when(in_bucket, Order.amount), _sum_when(in_bucket)))
    scope = Order.reseller_id.is_(None) if reseller_id is None else Order.reseller_id == reseller_id
    row = (await session.execute(select(*columns).where(
        scope, non_demo_customer(Order.user_id), Order.status == "delivered",
        Order.created_at >= window.start, Order.created_at < window.end,
    ))).one()
    points = [
        FinanceTrendPoint(report_datetime_label(start), report_datetime_label(end),
                          int(row[index * 2] or 0), int(row[index * 2 + 1] or 0))
        for index, (start, end) in enumerate(zip(starts, starts[1:], strict=False))
    ]
    peak = max((point.revenue for point in points), default=0)
    for index, point in enumerate(points):
        point.x = index * CHART_WIDTH // max(len(points) - 1, 1) if len(points) > 1 else CHART_WIDTH // 2
        point.y = CHART_HEIGHT - point.revenue * CHART_HEIGHT // max(peak, 1)
    return interval_label, points


async def build_finance_dashboard(
    session: AsyncSession, *, reseller_id: int | None, window: FinanceReportRange,
) -> FinanceDashboard:
    active, previous = await _period_metrics(session, reseller_id=reseller_id, window=window)
    interval, timeline = await _sales_timeline(session, reseller_id=reseller_id, window=window)
    methods = await shop_payment_method_breakdown(
        session, reseller_id=reseller_id, since=window.start, until=window.end,
    )
    previous_revenue = previous["revenue"]
    change = round(Fraction((active["revenue"] - previous_revenue) * 100, previous_revenue)) if previous_revenue else None
    return {
        "period": window.period, "period_label": PERIOD_LABELS[window.period], "currency": get_settings().currency,
        "window": window, "start_label": report_datetime_label(window.start),
        "end_label": report_datetime_label(window.end),
        "generated_at": report_datetime_label(datetime.now(timezone.utc)),
        "previous_label": report_datetime_label(window.previous_start),
        "active": active, "previous": previous, "change_percent": change,
        "timeline": timeline, "interval_label": interval,
        "chart_points": " ".join(f"{point.x},{point.y}" for point in timeline),
        "chart_peak": max((point.revenue for point in timeline), default=0),
        "payment_methods": methods,
        "ops": await shop_ops_snapshot(session, reseller_id=reseller_id),
    }
