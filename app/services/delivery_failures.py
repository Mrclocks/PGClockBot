"""Paid orders whose delivery failed or stalled: record, list, resolve and retry them."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    BotUser,
    DeliveryFailure,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
)
from app.services.delivery_recovery_policy import (
    DELIVERING_STALE_AFTER,
    DELIVERY_IN_PROGRESS,
    FIRST_AUTO_RETRY_DELAY,
    is_auto_recoverable_kind,
    is_delivered,
    missing_delivery_clause,
)
from app.services.pasarguard import failure_text, is_pg_not_found

logger = logging.getLogger(__name__)


async def record_delivery_failure(
    session: AsyncSession,
    *,
    order: Order,
    error: str,
    payment_id: int | None = None,
) -> DeliveryFailure:
    existing = (
        await session.execute(
            select(DeliveryFailure)
            .where(
                DeliveryFailure.order_id == int(order.id),
                DeliveryFailure.resolved_at.is_(None),
            )
            .order_by(DeliveryFailure.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    msg = (error or "")[:2000]
    now = datetime.now(timezone.utc)
    if existing:
        existing.error = msg
        existing.attempts = int(existing.attempts or 0) + 1
        existing.last_attempt_at = now
        if payment_id:
            existing.payment_id = int(payment_id)
        await session.flush()
        return existing
    auto = is_auto_recoverable_kind(order) and not is_delivered(order)
    row = DeliveryFailure(
        order_id=int(order.id),
        payment_id=int(payment_id) if payment_id else None,
        reseller_id=int(order.reseller_id) if order.reseller_id else None,
        error=msg,
        attempts=1,
        last_attempt_at=now,
        # Renewals and purchases are retried by paid_order_recovery; the rest stay with staff.
        next_auto_retry_at=now + FIRST_AUTO_RETRY_DELAY if auto else None,
        auto_retry_stopped_at=None if auto else now,
    )
    session.add(row)
    await session.flush()
    return row


async def note_delivery_send_failure(
    session: AsyncSession,
    *,
    order: Order | None,
    error: str,
    payment: Payment | None = None,
) -> None:
    """After approve: record DeliveryFailure and stamp payment.review_note."""
    if order is None:
        return
    try:
        await record_delivery_failure(
            session,
            order=order,
            error=error,
            payment_id=int(payment.id) if payment is not None else None,
        )
        if payment is not None:
            payment.review_note = payment.review_note or "delivery_failed"
        await session.commit()
    except Exception:
        logger.debug("note_delivery_send_failure failed", exc_info=True)


async def resolve_delivery_failure(session: AsyncSession, order_id: int) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(DeliveryFailure)
        .where(
            DeliveryFailure.order_id == int(order_id),
            DeliveryFailure.resolved_at.is_(None),
        )
        .values(resolved_at=now)
    )


async def list_stuck_paid_orders(
    session: AsyncSession, *, reseller_id: int | None = None, limit: int = 100
) -> list[Order]:
    """Paid orders not delivered yet (no service, or a renewal/add-on not applied) — need a retry."""
    from app.db.models import OrderStatus

    q = (
        select(Order)
        .where(
            Order.status.in_([OrderStatus.PAID.value, OrderStatus.DELIVERING.value]),
            missing_delivery_clause(),
        )
        .order_by(Order.id.desc())
        .limit(limit)
    )
    if reseller_id is None:
        q = q.where(Order.reseller_id.is_(None))
    else:
        q = q.where(Order.reseller_id == int(reseller_id))
    return list((await session.execute(q)).scalars().all())


async def count_stuck_paid_orders(
    session: AsyncSession, *, reseller_id: int | None = None
) -> int:
    from sqlalchemy import func

    from app.db.models import OrderStatus

    q = (
        select(func.count())
        .select_from(Order)
        .where(
            Order.status.in_([OrderStatus.PAID.value, OrderStatus.DELIVERING.value]),
            missing_delivery_clause(),
        )
    )
    if reseller_id is None:
        q = q.where(Order.reseller_id.is_(None))
    else:
        q = q.where(Order.reseller_id == int(reseller_id))
    return int((await session.scalar(q)) or 0)


async def list_open_delivery_failures(
    session: AsyncSession, *, reseller_id: int | None = None, limit: int = 100
) -> list[DeliveryFailure]:
    """Open failures, one row per order (the newest), newest attempt first."""
    newest = select(func.max(DeliveryFailure.id)).where(DeliveryFailure.resolved_at.is_(None))
    if reseller_id is None:
        newest = newest.where(DeliveryFailure.reseller_id.is_(None))
    else:
        newest = newest.where(DeliveryFailure.reseller_id == int(reseller_id))
    q = (
        select(DeliveryFailure)
        .where(DeliveryFailure.id.in_(newest.group_by(DeliveryFailure.order_id).scalar_subquery()))
        .order_by(DeliveryFailure.last_attempt_at.desc())
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


class DeliveryNotRetryable(ValueError):
    """The order can never be delivered again from the failure queue; the admin may dismiss it."""


# Short enough for a Telegram alert after the «تلاش مجدد ناموفق: » prefix.
SERVICE_USER_MISSING = (
    "کاربر این سرویس در پنل پیدا نشد (احتمالاً حذف شده)؛ تلاش دوباره فایده ندارد. "
    "«لغو و بازگشت وجه» یا «بستن بدون بازگشت وجه» را بزنید."
)


def _assert_retryable(order: Order) -> None:
    if order.status == OrderStatus.DELIVERED.value:
        if not order.service_id:
            raise DeliveryNotRetryable(
                "سرویس این سفارش دیگر وجود ندارد و چیزی برای ارسال نمانده؛ از «حذف از لیست» استفاده کنید"
            )
        return
    if order.status not in {
        OrderStatus.PAID.value,
        OrderStatus.DELIVERING.value,
        OrderStatus.AWAITING_APPROVAL.value,
    }:
        raise DeliveryNotRetryable("این سفارش برای تلاش مجدد آماده نیست؛ در صورت نیاز از «حذف از لیست» استفاده کنید")


RETRY_IN_PROGRESS = DELIVERY_IN_PROGRESS


NOT_PAID_FOR_RETRY = "برای این سفارش پرداخت تأییدشده‌ای نیست؛ اول پرداخت را تأیید کنید"


async def _reopen_for_retry(session: AsyncSession, order: Order) -> None:
    """Move a crashed DELIVERING (or approved-but-unclaimed) order back to PAID, atomically.

    A DELIVERING order that is still fresh may be mid-create (the retry job or another
    admin), and an order awaiting approval is only reopened when its payment was approved.
    """
    from app.db.models import Payment

    query = update(Order).where(Order.id == int(order.id))
    if order.status == OrderStatus.DELIVERING.value:
        query = query.where(
            Order.status == OrderStatus.DELIVERING.value,
            Order.updated_at <= datetime.now(timezone.utc) - DELIVERING_STALE_AFTER,
        )
    else:
        approved = await session.scalar(
            select(Payment.id).where(
                Payment.order_id == int(order.id), Payment.status == PaymentStatus.APPROVED.value
            ).limit(1)
        )
        if approved is None:
            raise DeliveryNotRetryable(NOT_PAID_FOR_RETRY)
        query = query.where(Order.status == order.status)
    result = await session.execute(
        query.values(status=OrderStatus.PAID.value).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ValueError(RETRY_IN_PROGRESS)
    await session.commit()
    await session.refresh(order)


async def retry_delivery(session: AsyncSession, order_id: int) -> Order:
    from app.db.models import Payment
    from app.services.delivery import send_delivery_to_user
    from app.services.orders import fulfill_paid_order
    from app.services.reseller_bots import open_notify_bot_for_user

    order = (
        await session.execute(
            select(Order)
            .where(Order.id == int(order_id))
            .options(selectinload(Order.plan), selectinload(Order.user))
        )
    ).scalar_one_or_none()
    if not order:
        raise ValueError("سفارش پیدا نشد")
    # Checked before the try below: an order that can never be retried must not be
    # recorded as one more failed attempt every time somebody presses the button.
    _assert_retryable(order)

    async def _resend() -> None:
        user = order.__dict__.get("user") or await session.get(BotUser, order.user_id)
        if not user or not user.telegram_id:
            return
        pay = (
            await session.execute(
                select(Payment)
                .where(Payment.order_id == int(order.id))
                .order_by(Payment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        bot, should_close = await open_notify_bot_for_user(session, user, shop_id=order.reseller_id)
        try:
            await send_delivery_to_user(
                bot, user.telegram_id, session, pay, order
            )
        finally:
            if should_close:
                try:
                    await bot.session.close()
                except Exception:
                    logger.debug("_resend: ignored failure", exc_info=True)

    resend_only = order.status == OrderStatus.DELIVERED.value and bool(order.service_id)
    if not resend_only and order.status != OrderStatus.PAID.value:
        # Before the try: a refusal here is not one more failed delivery attempt.
        await _reopen_for_retry(session, order)
    try:
        if resend_only:
            # Resend Telegram delivery only
            await _resend()
        else:
            # Route renew/addon/reseller_app correctly — deliver_order alone
            # seals mutation orders (service_id already set) without applying.
            order = await fulfill_paid_order(session, order)
            try:
                await _resend()
            except Exception as send_exc:
                await record_delivery_failure(session, order=order, error=str(send_exc))
                await session.commit()
                raise
        await resolve_delivery_failure(session, int(order.id))
        await session.commit()
        return order
    except Exception as exc:
        await record_delivery_failure(session, order=order, error=failure_text(exc))
        await session.commit()
        if order.service_id and is_pg_not_found(exc):
            # A renewal or pack for a panel user that is gone: the same press will fail the same way.
            raise ValueError(SERVICE_USER_MISSING) from exc
        raise
