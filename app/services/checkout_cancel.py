"""A customer cancels their own unfinished purchase from the bot.

Only an order nobody has paid for may be cancelled this way: no approved payment, no receipt sent, and no online
payment (gateway, PSP, crypto, Stars, BezanBeCard) still in progress, whose result could arrive after the cancel.
The order is found by its id, the customer and the shop whose bot is open, never by the id alone. Cancelling goes
through ``orders.cancel_order``: an atomic status claim, pending payments rejected, codes, rules and trial claims
released. A random-discount amount stays reserved until its window ends, so a transfer made before the cancel
cannot be mistaken for another customer's order.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Payment, PaymentMethod, PaymentStatus
from app.services.orders import cancel_order

CUSTOMER_CANCEL_NOTE = "cancelled by the customer"
CUSTOMER_CANCEL_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.AWAITING_RECEIPT.value,
    OrderStatus.REJECTED.value,
)
NOT_FOUND = "این سفارش یافت نشد"
NOT_CANCELLABLE = "این سفارش دیگر قابل لغو نیست؛ اگر پرداخت کرده‌اید منتظر بررسی بمانید یا با پشتیبانی تماس بگیرید"


def _payment_in_progress(order_id: object):
    """A payment of the order that may move money: approved, with a receipt, or an online payment still open."""
    return exists(
        select(Payment.id).where(
            Payment.order_id == order_id,
            or_(
                Payment.status == PaymentStatus.APPROVED.value,
                and_(
                    Payment.status == PaymentStatus.PENDING.value,
                    or_(
                        Payment.receipt_file_id.is_not(None),
                        Payment.receipt_at.is_not(None),
                        Payment.method != PaymentMethod.CARD.value,
                    ),
                ),
            ),
        )
    )


def customer_cancellable_clause(order_id: object):
    """SQL condition: the order (by id column or value) is one its customer may still cancel."""
    return and_(
        Order.status.in_(CUSTOMER_CANCEL_STATUSES),
        Order.service_id.is_(None),
        ~_payment_in_progress(order_id),
    )


async def is_customer_cancellable(session: AsyncSession, order_id: int) -> bool:
    found = await session.scalar(
        select(Order.id).where(Order.id == int(order_id), customer_cancellable_clause(Order.id))
    )
    return found is not None


async def cancel_customer_checkout(
    session: AsyncSession, *, user_id: int, order_id: int, shop_id: int | None
) -> Order:
    """Cancel the customer's own unpaid order in this shop; ``ValueError`` with a safe reason otherwise."""
    shop = Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)
    order = await session.scalar(
        select(Order).where(Order.id == int(order_id), Order.user_id == int(user_id), shop)
    )
    if order is None:
        raise ValueError(NOT_FOUND)
    try:
        # The condition is part of the status claim, so a receipt arriving meanwhile stops the cancel.
        return await cancel_order(
            session, order, note=CUSTOMER_CANCEL_NOTE, only_if=customer_cancellable_clause(Order.id)
        )
    except ValueError:
        await session.rollback()
        raise ValueError(NOT_CANCELLABLE) from None
