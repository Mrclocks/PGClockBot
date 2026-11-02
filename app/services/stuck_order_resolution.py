"""Close a paid order that never got a service: refund it, record a manual delivery, or close it.

Such an order sits in «تحویل ناموفق» as «گیرکرده». Every action claims the order with one atomic
UPDATE (status paid/delivering, no service, not touched for a while), so pressing twice, or two
admins at once, changes it once; the refund and the status change are one transaction.

Renewal, add-on and reseller-application orders are closed here too. A refund is refused only
when the panel may already have applied the change (an attempt with an unknown outcome) or the
application it paid for was approved; those can still be closed without moving money.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    DeliveryFailure,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    ResellerApplication,
    ResellerApplicationStatus,
)
from app.services.orders import is_mutation_order_note
from app.services.shop_scope import assert_order_retry_in_scope
from app.services.wallet import credit_wallet

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
STUCK_STATUSES = (OrderStatus.PAID.value, OrderStatus.DELIVERING.value)
ORDER_NOT_FOUND = "سفارش پیدا نشد"
NOT_STUCK = "این سفارش دیگر گیرکرده نیست یا همین حالا در حال تحویل است؛ چند دقیقه بعد دوباره امتحان کنید"
MUTATION_MAY_BE_APPLIED = (
    "آخرین تلاشِ اعمال این تمدید یا بسته نتیجهٔ نامعلومی داشت و شاید در پاسارگارد اعمال شده باشد؛ "
    "بازگشت وجه انجام نشد. سرویس را در پنل بررسی کنید و بعد «تحویل دستی انجام شد» یا «بستن بدون بازگشت وجه» را بزنید"
)
APPLICATION_APPROVED = (
    "درخواست نمایندگیِ این سفارش تأیید شده و نمایندگی فعال است؛ بازگشت وجه انجام نشد. "
    "اگر لازم است، از «بستن بدون بازگشت وجه» استفاده کنید"
)
OPEN_APPLICATION_STATUSES = (
    ResellerApplicationStatus.PENDING_PAYMENT.value,
    ResellerApplicationStatus.AWAITING_APPROVAL.value,
)


@dataclass(frozen=True, slots=True)
class Resolution:
    order_id: int
    user_id: int
    refunded: int
    shop_id: int | None


async def _load(session: AsyncSession, staff: dict, order_id: int) -> Order:
    order = await session.get(Order, int(order_id))
    if order is None:
        raise ValueError(ORDER_NOT_FOUND)
    assert_order_retry_in_scope(staff, order)
    return order


def _mutation_may_be_applied(order: Order) -> bool:
    """A renewal or add-on whose last panel write may have gone through."""
    if not is_mutation_order_note(order.note):
        return False
    if order.service_mutation_pending:
        return True
    try:
        snapshot = json.loads(order.renewal_snapshot) if order.renewal_snapshot else None
    except ValueError:
        return True
    return isinstance(snapshot, dict) and bool(snapshot.get("target"))


async def _application(session: AsyncSession, order: Order) -> ResellerApplication | None:
    note = (order.note or "").strip()
    if not note.startswith("reseller_app:"):
        return None
    try:
        return await session.get(ResellerApplication, int(note.split(":", 1)[1]))
    except ValueError:
        return None


async def _assert_refund_safe(session: AsyncSession, order: Order) -> None:
    if _mutation_may_be_applied(order):
        raise ValueError(MUTATION_MAY_BE_APPLIED)
    app = await _application(session, order)
    if app is not None and app.status == ResellerApplicationStatus.APPROVED.value:
        raise ValueError(APPLICATION_APPROVED)


async def _close_application(session: AsyncSession, order: Order, actor: str) -> None:
    """A closed application order leaves no application waiting for a payment it will not get."""
    app = await _application(session, order)
    if app is not None and app.status in OPEN_APPLICATION_STATUSES:
        app.status = ResellerApplicationStatus.CANCELLED.value
        app.admin_note = (app.admin_note or f"سفارش #{int(order.id)} از لیست تحویل ناموفق بسته شد ({actor})")[:2000]


async def _claim(session: AsyncSession, order: Order, new_status: str, now: datetime) -> None:
    result = await session.execute(
        update(Order)
        .where(
            Order.id == int(order.id),
            Order.status.in_(STUCK_STATUSES),
            Order.service_id.is_(None),
            Order.updated_at <= now - STALE_AFTER,
        )
        # A closed renewal or add-on no longer blocks other changes to its service.
        .values(status=new_status, service_mutation_pending=False),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ValueError(NOT_STUCK)


async def _approved_payments(session: AsyncSession, order_id: int) -> list[Payment]:
    rows = await session.scalars(
        select(Payment).where(
            Payment.order_id == int(order_id),
            Payment.status == PaymentStatus.APPROVED.value,
            Payment.is_wallet_topup.is_(False),
        ).order_by(Payment.id)
    )
    return list(rows.all())


async def _close_failures(session: AsyncSession, order_id: int, now: datetime) -> None:
    await session.execute(
        update(DeliveryFailure)
        .where(DeliveryFailure.order_id == int(order_id), DeliveryFailure.resolved_at.is_(None))
        .values(resolved_at=now),
        execution_options={"synchronize_session": False},
    )


def _note(payment: Payment | None, text: str) -> None:
    if payment is not None:
        payment.review_note = f"{(payment.review_note or '').strip()} | {text}".strip(" |")[:2000]


async def cancel_and_refund(session: AsyncSession, staff: dict, order_id: int, *, actor: str, now: datetime | None = None) -> Resolution:
    """Cancel the order and return what was paid for it to the customer's wallet (once)."""
    now = now or datetime.now(timezone.utc)
    order = await _load(session, staff, order_id)
    await _assert_refund_safe(session, order)
    user_id, shop_id = int(order.user_id), (int(order.reseller_id) if order.reseller_id else None)
    payments = await _approved_payments(session, order_id)
    amount = sum(max(0, int(p.amount or 0)) for p in payments)
    await _claim(session, order, OrderStatus.CANCELLED.value, now)
    await _close_application(session, order, actor)
    if amount > 0:
        user = await session.get(BotUser, user_id)
        if user is None:
            await session.rollback()
            raise ValueError(ORDER_NOT_FOUND)
        await credit_wallet(session, user, amount, f"بازگشت وجه سفارش #{int(order_id)} (تحویل انجام نشد)", shop_id=shop_id, commit=False)
    _note(payments[-1] if payments else None, f"refunded {amount} to wallet by {actor}")
    await _close_failures(session, order_id, now)
    await session.commit()
    logger.info("stuck order %s cancelled and refunded amount=%s by=%s", order_id, amount, actor)
    return Resolution(order_id=int(order_id), user_id=user_id, refunded=amount, shop_id=shop_id)


async def mark_delivered_manually(session: AsyncSession, staff: dict, order_id: int, *, actor: str, now: datetime | None = None) -> Resolution:
    """Record that the admin delivered the service by hand; no money moves."""
    now = now or datetime.now(timezone.utc)
    order = await _load(session, staff, order_id)
    user_id, shop_id = int(order.user_id), (int(order.reseller_id) if order.reseller_id else None)
    payments = await _approved_payments(session, order_id)
    await _claim(session, order, OrderStatus.DELIVERED.value, now)
    _note(payments[-1] if payments else None, f"delivered manually, confirmed by {actor}")
    await _close_failures(session, order_id, now)
    await session.commit()
    logger.info("stuck order %s marked delivered manually by=%s", order_id, actor)
    return Resolution(order_id=int(order_id), user_id=user_id, refunded=0, shop_id=shop_id)


async def close_without_refund(session: AsyncSession, staff: dict, order_id: int, *, actor: str, now: datetime | None = None) -> Resolution:
    """Cancel the order and take it off the list without moving money (already settled elsewhere)."""
    now = now or datetime.now(timezone.utc)
    order = await _load(session, staff, order_id)
    user_id, shop_id = int(order.user_id), (int(order.reseller_id) if order.reseller_id else None)
    payments = await _approved_payments(session, order_id)
    await _claim(session, order, OrderStatus.CANCELLED.value, now)
    await _close_application(session, order, actor)
    _note(payments[-1] if payments else None, f"closed without refund by {actor}")
    await _close_failures(session, order_id, now)
    await session.commit()
    logger.info("stuck order %s closed without refund by=%s", order_id, actor)
    return Resolution(order_id=int(order_id), user_id=user_id, refunded=0, shop_id=shop_id)


@dataclass(frozen=True, slots=True)
class StuckReason:
    kind: str  # «خرید» / «تمدید» / ...
    reason: str
    can_refund: bool


APPLICATION_REASONS = {
    ResellerApplicationStatus.PENDING_PAYMENT.value: "پرداخت شده ولی درخواست نمایندگی هنوز «در انتظار پرداخت» مانده است",
    ResellerApplicationStatus.AWAITING_APPROVAL.value: "پرداخت شده؛ درخواست نمایندگی منتظر تأیید یا ساخت حساب است",
    ResellerApplicationStatus.APPROVED.value: "درخواست نمایندگی تأیید شده ولی سفارش بسته نشده است",
    ResellerApplicationStatus.REJECTED.value: "درخواست نمایندگی رد شده ولی پول آن برنگشته است",
    ResellerApplicationStatus.CANCELLED.value: "درخواست نمایندگی لغو شده ولی پول آن برنگشته است",
}


def _order_kind(note: str) -> str:
    if note.startswith("renew:"):
        return "تمدید"
    if note.startswith("svc_addon:"):
        return "بستهٔ افزودنی"
    if note.startswith("reseller_app:"):
        return "درخواست نمایندگی"
    return "خرید سرویس"


def _reason_text(order: Order, app: ResellerApplication | None) -> str:
    note = (order.note or "").strip()
    if note.startswith("reseller_app:"):
        return APPLICATION_REASONS.get(app.status if app else "", "درخواست نمایندگیِ این سفارش پیدا نشد")
    if is_mutation_order_note(note):
        if _mutation_may_be_applied(order):
            return "آخرین تلاش برای اعمال در پاسارگارد نتیجهٔ نامعلومی داشت"
        return "سرویسی که باید تمدید یا افزوده شود دیگر وجود ندارد (حذف یا لغو شده)"
    if order.status == OrderStatus.DELIVERING.value:
        return "ساخت سرویس شروع شد ولی تمام نشد"
    return "پرداخت شده ولی سرویسی ساخته نشد"


async def describe_stuck_orders(session: AsyncSession, orders: list[Order]) -> dict[int, StuckReason]:
    """Plain reason, kind and refund availability per stuck order (one query for applications)."""
    app_ids: dict[int, int] = {}
    for order in orders:
        note = (order.note or "").strip()
        if note.startswith("reseller_app:") and note.split(":", 1)[1].isdigit():
            app_ids[int(order.id)] = int(note.split(":", 1)[1])
    apps: dict[int, ResellerApplication] = {}
    if app_ids:
        rows = await session.scalars(select(ResellerApplication).where(ResellerApplication.id.in_(set(app_ids.values()))))
        apps = {int(a.id): a for a in rows}
    out: dict[int, StuckReason] = {}
    for order in orders:
        app = apps.get(app_ids.get(int(order.id), 0))
        out[int(order.id)] = StuckReason(
            kind=_order_kind((order.note or "").strip()),
            reason=_reason_text(order, app),
            can_refund=not _mutation_may_be_applied(order)
            and not (app is not None and app.status == ResellerApplicationStatus.APPROVED.value),
        )
    return out


async def count_still_stuck(session: AsyncSession, order_ids: list[int]) -> int:
    """How many of these orders are still paid without a service (they stay in the stuck list)."""
    if not order_ids:
        return 0
    return int(
        await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.id.in_([int(i) for i in order_ids]), Order.status.in_(STUCK_STATUSES), Order.service_id.is_(None)
            )
        )
        or 0
    )
