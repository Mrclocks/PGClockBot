"""A customer's own orders in one shop, with what to do next for each.

Every query is scoped by the customer and the shop whose bot asks; an order id of someone else, or of
another shop, reads as missing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Payment, PaymentStatus, Plan

PAGE_SIZE = 8
DELIVERY_DELAY_MINUTES = 15
KIND_PURCHASE = "خرید"
KIND_RENEWAL = "تمدید"
KIND_ADDON = "افزونه"
STATUS_TEXT = {
    OrderStatus.PENDING.value: "پرداخت هنوز شروع نشده",
    OrderStatus.AWAITING_RECEIPT.value: "منتظر رسید شما",
    OrderStatus.AWAITING_APPROVAL.value: "رسید ارسال شد؛ در انتظار بررسی",
    OrderStatus.PAID.value: "پرداخت شد؛ در حال آماده‌سازی",
    OrderStatus.DELIVERING.value: "در حال تحویل",
    OrderStatus.DELIVERED.value: "تحویل شد",
    OrderStatus.REJECTED.value: "پرداخت رد شد",
    OrderStatus.CANCELLED.value: "لغو شد",
}
WAITING_FOR_RECEIPT = (OrderStatus.PENDING.value, OrderStatus.AWAITING_RECEIPT.value)
DELAYABLE = (OrderStatus.PAID.value, OrderStatus.DELIVERING.value)


@dataclass(slots=True)
class CustomerOrder:
    order_id: int
    kind: str
    plan_name: str | None
    amount: int
    status: str
    created_at: datetime | None
    payment_status: str | None
    waiting_receipt: bool
    delayed: bool

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)


def order_kind(order: Order) -> str:
    note = (order.note or "").strip()
    if note.startswith("renew:"):
        return KIND_RENEWAL
    if note.startswith("svc_addon:"):
        return KIND_ADDON
    return KIND_PURCHASE


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _delayed(order: Order, now: datetime) -> bool:
    changed = _aware(order.updated_at) or _aware(order.created_at)
    return order.status in DELAYABLE and changed is not None and now - changed > timedelta(minutes=DELIVERY_DELAY_MINUTES)


def _shop_clause(shop_id: int | None):
    return Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)


async def _latest_payments(session: AsyncSession, order_ids: list[int]) -> dict[int, Payment]:
    """The newest payment of each order, from one query."""
    if not order_ids:
        return {}
    rows = await session.scalars(select(Payment).where(Payment.order_id.in_(order_ids)).order_by(Payment.id))
    return {int(payment.order_id): payment for payment in rows}


async def _build(session: AsyncSession, rows: list[tuple[Order, str | None]]) -> list[CustomerOrder]:
    payments = await _latest_payments(session, [int(order.id) for order, _ in rows])
    now = datetime.now(timezone.utc)
    items = []
    for order, plan_name in rows:
        payment = payments.get(int(order.id))
        waiting = bool(
            order.status in WAITING_FOR_RECEIPT
            and payment is not None
            and payment.status == PaymentStatus.PENDING.value
            and not payment.receipt_file_id
        )
        items.append(
            CustomerOrder(
                order_id=int(order.id), kind=order_kind(order), plan_name=plan_name, amount=int(order.amount),
                status=order.status, created_at=_aware(order.created_at),
                payment_status=payment.status if payment else None, waiting_receipt=waiting, delayed=_delayed(order, now),
            )
        )
    return items


async def list_customer_orders(
    session: AsyncSession, user_id: int, *, shop_id: int | None, before_id: int | None = None, limit: int = PAGE_SIZE
) -> tuple[list[CustomerOrder], int | None]:
    """Newest orders first; returns (items, cursor for the next older page)."""
    limit = max(1, min(int(limit), 20))
    query = (
        select(Order, Plan.name)
        .outerjoin(Plan, Plan.id == Order.plan_id)
        .where(Order.user_id == int(user_id), _shop_clause(shop_id))
        .order_by(Order.id.desc())
        .limit(limit + 1)
    )
    if before_id is not None:
        query = query.where(Order.id < int(before_id))
    rows = [(order, name) for order, name in (await session.execute(query)).all()]
    next_cursor = int(rows[limit - 1][0].id) if len(rows) > limit else None
    return await _build(session, rows[:limit]), next_cursor


async def get_customer_order(session: AsyncSession, user_id: int, order_id: int, *, shop_id: int | None) -> CustomerOrder | None:
    row = (
        await session.execute(
            select(Order, Plan.name)
            .outerjoin(Plan, Plan.id == Order.plan_id)
            .where(Order.id == int(order_id), Order.user_id == int(user_id), _shop_clause(shop_id))
        )
    ).first()
    return (await _build(session, [(row[0], row[1])]))[0] if row else None
