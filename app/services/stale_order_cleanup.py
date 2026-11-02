"""Policy, preview and run record for cancelling abandoned pending orders.

The cancel itself lives in ``app.services.orders``; this module decides which orders
count as abandoned (from the shop's or platform's settings) and keeps the last run
visible to the operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus

MIN_HOURS = 1
MAX_HOURS = 720
DEFAULT_HOURS = 48
DEFAULT_APPROVAL_HOURS = 72
KEY_LAST_RUN = "pending_order_cleanup_last_run"
KEY_LAST_COUNT = "pending_order_cleanup_last_count"

UNPAID_STATUSES = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.AWAITING_RECEIPT.value,
        OrderStatus.REJECTED.value,
    }
)


@dataclass(frozen=True, slots=True)
class StaleOrderPolicy:
    """``approval_hours`` is None when orders waiting for receipt approval are never cancelled."""

    hours: int
    approval_hours: int | None = None


def clamp_hours(raw: object, default: int) -> int:
    try:
        hours = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        hours = default
    return max(MIN_HOURS, min(MAX_HOURS, hours))


def policy_from_settings(ui: dict[str, str]) -> StaleOrderPolicy:
    from app.services.users import on

    hours = clamp_hours(ui.get("pending_order_ttl_hours"), DEFAULT_HOURS)
    approval_hours = None
    if on(ui.get("pending_order_cleanup_awaiting_approval", "0")):
        approval_hours = clamp_hours(ui.get("pending_order_approval_ttl_hours"), DEFAULT_APPROVAL_HOURS)
    return StaleOrderPolicy(hours=hours, approval_hours=approval_hours)


def cancellable_statuses(policy: StaleOrderPolicy) -> frozenset[str]:
    if policy.approval_hours is None:
        return UNPAID_STATUSES
    return UNPAID_STATUSES | {OrderStatus.AWAITING_APPROVAL.value}


def stale_orders_clause(policy: StaleOrderPolicy, *, now: datetime | None = None) -> ColumnElement[bool]:
    now = now or datetime.now(timezone.utc)
    stale_unpaid = and_(
        Order.status.in_(tuple(UNPAID_STATUSES)),
        Order.created_at < now - timedelta(hours=policy.hours),
    )
    if policy.approval_hours is None:
        return stale_unpaid
    stale_awaiting = and_(
        Order.status == OrderStatus.AWAITING_APPROVAL.value,
        Order.created_at < now - timedelta(hours=policy.approval_hours),
    )
    return or_(stale_unpaid, stale_awaiting)


def scope_to_shop(query, reseller_id: int | None):
    if reseller_id is None:
        return query.where(Order.reseller_id.is_(None))
    return query.where(Order.reseller_id == int(reseller_id))


async def count_stale_orders(
    session: AsyncSession, policy: StaleOrderPolicy, *, reseller_id: int | None
) -> int:
    query = select(func.count(Order.id)).where(stale_orders_clause(policy))
    return int(await session.scalar(scope_to_shop(query, reseller_id)) or 0)


async def record_cleanup_run(session: AsyncSession, *, cancelled: int, reseller_id: int | None) -> None:
    from app.services.users import set_settings_bulk

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await set_settings_bulk(
        session, {KEY_LAST_RUN: stamp, KEY_LAST_COUNT: str(int(cancelled))}, reseller_id=reseller_id
    )


async def cleanup_status(session: AsyncSession, ui: dict[str, str], *, reseller_id: int | None) -> dict[str, object]:
    """What the cleanup tab shows: orders ready to cancel now and the last run."""
    policy = policy_from_settings(ui)
    last_run = (ui.get(KEY_LAST_RUN) or "").strip()
    last_count = str(ui.get(KEY_LAST_COUNT) or "0")
    return {
        "ready_count": await count_stale_orders(session, policy, reseller_id=reseller_id),
        "last_run": last_run.replace("T", " ")[:16] if last_run else "",
        "last_count": int(last_count) if last_count.isdigit() else 0,
        "hours": policy.hours,
        "approval_hours": policy.approval_hours,
    }
