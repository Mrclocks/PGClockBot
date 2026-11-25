"""Unfinished purchases that hold a random-discount amount, and releasing the ones the customer walked away from.

An order is *abandoned* when it is still ``pending`` and has no payment row at all: the customer saw the payment
methods and never chose one, so no money can be on its way. Starting a new purchase may cancel such orders of the
same customer (``release_abandoned_orders``) instead of refusing the new one. The cancel is an atomic status claim
that re-checks «pending, no payment» at the moment of writing; codes, rules, custom plans and ledger lines are
given back in the caller's transaction (nothing here commits). The reserved amount itself stays reserved until
its window ends.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Payment, PaymentStatus
from app.services.custom_plans import discard_unsold_custom_plan
from app.services.order_discounts import release_order_discounts

ABANDONED_LIMIT = 10
OPEN_DRAW_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.AWAITING_RECEIPT.value,
    OrderStatus.AWAITING_APPROVAL.value,
)
WAIT_MINUTES_MIN = 1


def abandoned_clause() -> ColumnElement[bool]:
    """``Order`` rows still pending with no payment of any kind."""
    return and_(
        Order.status == OrderStatus.PENDING.value,
        Order.service_id.is_(None),
        ~exists(select(Payment.id).where(Payment.order_id == Order.id)),
    )


def nothing_paid_clause() -> ColumnElement[bool]:
    """``Order`` rows without an approved payment or a receipt (a cancelled order may still be reused)."""
    return ~exists(
        select(Payment.id).where(
            Payment.order_id == Order.id,
            (Payment.status == PaymentStatus.APPROVED.value)
            | Payment.receipt_file_id.is_not(None)
            | Payment.receipt_at.is_not(None),
        )
    )


async def release_abandoned_orders(session: AsyncSession, order_ids: list[int], *, user_id: int) -> list[int]:
    """Cancel the customer's abandoned orders among ``order_ids``; the ids actually cancelled, oldest first."""
    if not order_ids:
        return []
    claimed = (
        await session.execute(
            update(Order)
            .where(Order.id.in_(order_ids[:ABANDONED_LIMIT]), Order.user_id == int(user_id), abandoned_clause())
            .values(status=OrderStatus.CANCELLED.value)
            .returning(Order.id)
            .execution_options(synchronize_session=False)
        )
    ).scalars().all()
    cancelled = sorted(int(order_id) for order_id in claimed)
    if not cancelled:
        return []
    # Deferred: discount_rules imports random_discounts, which imports this module.
    from app.services.discount_rules import release_rule_discount

    orders = (await session.scalars(select(Order).where(Order.id.in_(cancelled)))).all()
    for order in orders:
        await release_rule_discount(session, int(order.id))
        await release_order_discounts(session, int(order.id))
        await discard_unsold_custom_plan(session, order)
    await session.flush()
    return cancelled


def wait_minutes(until: datetime, now: datetime) -> int:
    """Whole minutes from ``now`` until ``until``, at least one."""
    seconds = (until - now).total_seconds()
    return max(WAIT_MINUTES_MIN, int(-(-seconds // 60)))


def open_draws_message(until: datetime | None, now: datetime) -> str:
    """Why a new discounted purchase is refused, and what the customer can do now."""
    when = f"حدود {wait_minutes(until, now)} دقیقه دیگر دوباره تلاش کنید، یا " if until is not None else ""
    return (
        "چند خرید نیمه‌تمام با تخفیف دارید. "
        f"{when}از «کیف پول ← تراکنش‌ها ← 🧾 سفارش‌های من» خریدهایی را که پرداخت نکرده‌اید لغو کنید."
    )
