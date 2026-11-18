"""Finish paid orders whose delivery failed: retry while the panel answers, then refund once.

A failed delivery is listed in ``delivery_failures``. For a renewal or a new purchase (see
``delivery_recovery_policy``) the row is scheduled: each due row is leased with an atomic
UPDATE, retried through ``fulfill_paid_order`` (which claims the order itself), and either
delivered, rescheduled with backoff, or given up. Giving up refunds the payment to the
customer's wallet of the order's shop, unless the panel may already hold the change; that
case, and a new purchase whose create had an unknown outcome, goes to the order's staff.

Attempts are only spent while the panel answers; a long outage ends at the time window.
Notices are sent after the commit that decided them, and only by the run that decided them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryFailure, Order, OrderStatus, Payment, PaymentStatus, Plan
from app.py_compat import StrEnum
from app.services.db_safe import rollback_quiet
from app.services.delivery_recovery_policy import (
    DELIVERING_STALE_AFTER,
    AUTO_RETRY_WINDOW_HOURS,
    MAX_AUTO_ATTEMPTS,
    is_auto_recoverable_kind,
    is_delivered,
    is_new_purchase_order,
    is_renewal_order,
    payment_outcome_unknown,
)
from app.services.orders import (
    DeliveryAttempt,
    fulfill_paid_order,
    hold_unknown_delivery_for_review,
)
from app.services.stuck_order_resolution import (
    Resolution,
    mutation_may_be_applied,
    refund_undelivered_order,
    tell_customer_refunded,
)
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from app.services.ux20 import record_delivery_failure

logger = logging.getLogger(__name__)

RECOVERY_BATCH = 20
# Held while one run works on a row; a crashed run's row becomes due again after it.
ATTEMPT_LEASE = timedelta(minutes=10)
PANEL_DOWN_RECHECK = timedelta(minutes=2)
# Wait before attempt n+1 (the last value repeats).
RETRY_BACKOFF = (
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=10),
    timedelta(minutes=20),
    timedelta(minutes=30),
)
RETRY_WINDOW = timedelta(hours=AUTO_RETRY_WINDOW_HOURS)
# ShopBotUnavailable is a ValueError: a shop without its own bot cannot be messaged.
NOTICE_ERRORS = (TelegramAPIError, SQLAlchemyError, ValueError)

REASON_EXHAUSTED = "چند تلاش خودکار انجام شد ولی پاسارگارد سفارش را اعمال نکرد"
REASON_WINDOW = "در مهلت تلاش خودکار، پاسارگارد در دسترس نبود یا سفارش را اعمال نکرد"
REASON_MAY_BE_APPLIED = "نتیجهٔ آخرین تلاش در پاسارگارد نامعلوم است؛ شاید تمدید اعمال شده باشد"
REASON_CREATE_UNKNOWN = "پاسارگارد به ساخت سرویس پاسخ نداد؛ شاید سرویس ساخته شده باشد"


class Outcome(StrEnum):
    DELIVERED = "delivered"
    REFUNDED = "refunded"
    STAFF = "staff"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    order_id: int
    outcome: Outcome
    reason: str = ""
    resolution: Resolution | None = None


def retry_delay(attempts_made: int) -> timedelta:
    return RETRY_BACKOFF[min(max(attempts_made, 1), len(RETRY_BACKOFF)) - 1]


async def due_failure_ids(session: AsyncSession, *, now: datetime, limit: int = RECOVERY_BATCH) -> list[int]:
    rows = await session.scalars(
        select(DeliveryFailure.id)
        .where(
            DeliveryFailure.resolved_at.is_(None),
            DeliveryFailure.auto_retry_stopped_at.is_(None),
            DeliveryFailure.next_auto_retry_at.is_not(None),
            DeliveryFailure.next_auto_retry_at <= now,
        )
        .order_by(DeliveryFailure.next_auto_retry_at, DeliveryFailure.id)
        .limit(limit)
    )
    return list(rows.all())


def _open_auto(failure_id: int):
    return (
        DeliveryFailure.id == int(failure_id),
        DeliveryFailure.resolved_at.is_(None),
        DeliveryFailure.auto_retry_stopped_at.is_(None),
    )


async def _set_failure(session: AsyncSession, failure_id: int, *conditions, **values) -> bool:
    result = await session.execute(
        update(DeliveryFailure)
        .where(*_open_auto(failure_id), *conditions)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def _lease(session: AsyncSession, failure_id: int, now: datetime) -> bool:
    """Take the row for this run; False when another run or a person got it first."""
    return await _set_failure(
        session, failure_id, DeliveryFailure.next_auto_retry_at <= now,
        next_auto_retry_at=now + ATTEMPT_LEASE,
    )


async def _reschedule(session: AsyncSession, failure_id: int, when: datetime) -> None:
    await _set_failure(session, failure_id, next_auto_retry_at=when)


async def _stop(session: AsyncSession, failure_id: int, now: datetime) -> None:
    """End automatic handling; the row stays open in «تحویل ناموفق» for staff."""
    await _set_failure(session, failure_id, auto_retry_stopped_at=now, next_auto_retry_at=None)


async def _approved_payment(session: AsyncSession, order_id: int) -> Payment | None:
    return await session.scalar(
        select(Payment)
        .where(
            Payment.order_id == int(order_id),
            Payment.status == PaymentStatus.APPROVED.value,
            Payment.is_wallet_topup.is_(False),
        )
        .order_by(Payment.id.desc())
        .limit(1)
    )


def _is_recoverable(order: Order | None, payment: Payment | None) -> bool:
    """Still a paid, undelivered order of a kind a retry cannot apply twice."""
    if order is None or payment is None:
        return False
    if order.status not in (OrderStatus.PAID.value, OrderStatus.DELIVERING.value):
        return False
    if not is_auto_recoverable_kind(order):
        return False
    return not (is_new_purchase_order(order) and payment_outcome_unknown(payment))


async def _reopen_stale_renewal(session: AsyncSession, order: Order, now: datetime) -> bool:
    """A renewal left DELIVERING by a crash goes back to PAID; its saved target makes a retry safe."""
    result = await session.execute(
        update(Order)
        .where(
            Order.id == int(order.id),
            Order.status == OrderStatus.DELIVERING.value,
            Order.updated_at <= now - DELIVERING_STALE_AFTER,
        )
        .values(status=OrderStatus.PAID.value)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(order)
    return result.rowcount == 1


async def _give_up(
    session: AsyncSession, failure_id: int, order: Order, *, now: datetime, reason: str
) -> RecoveryResult | None:
    """Refund when nothing reached the panel; otherwise hand the order to its staff."""
    if is_renewal_order(order) and mutation_may_be_applied(order):
        await _stop(session, failure_id, now)
        return RecoveryResult(int(order.id), Outcome.STAFF, REASON_MAY_BE_APPLIED)
    order_id = int(order.id)
    if order.status != OrderStatus.PAID.value:
        # Mid-write elsewhere; look again once that attempt is over.
        await _reschedule(session, failure_id, now + PANEL_DOWN_RECHECK)
        return None
    try:
        resolution = await refund_undelivered_order(session, order, now=now)
    except ValueError as exc:
        # The order changed under us (staff acted) or a write is now in doubt.
        await rollback_quiet(session)
        logger.warning("auto refund refused order=%s: %s", order_id, exc)
        await _stop(session, failure_id, now)
        return RecoveryResult(order_id, Outcome.STAFF, str(exc))
    return RecoveryResult(order_id, Outcome.REFUNDED, reason, resolution)


async def _after_failed_attempt(
    session: AsyncSession, failure_id: int, order: Order, *, attempt: DeliveryAttempt, error: str, now: datetime
) -> RecoveryResult | None:
    await session.refresh(order)
    if is_new_purchase_order(order) and attempt.panel_may_hold_account:
        # Another create could give the buyer a second account: staff decide.
        await hold_unknown_delivery_for_review(session, order, error=error)
        await _stop(session, failure_id, now)
        return RecoveryResult(int(order.id), Outcome.STAFF, REASON_CREATE_UNKNOWN)
    await record_delivery_failure(session, order=order, error=error)
    await session.commit()
    failure = await session.get(DeliveryFailure, failure_id, populate_existing=True)
    attempts = int(failure.auto_attempts or 0) if failure else MAX_AUTO_ATTEMPTS
    if attempts >= MAX_AUTO_ATTEMPTS:
        return await _give_up(session, failure_id, order, now=now, reason=REASON_EXHAUSTED)
    await _reschedule(session, failure_id, now + retry_delay(attempts))
    return None


async def _attempt(session: AsyncSession, failure_id: int, order: Order, *, now: datetime) -> RecoveryResult | None:
    counted = await _set_failure(session, failure_id, auto_attempts=DeliveryFailure.auto_attempts + 1)
    if not counted:
        return None
    attempt = DeliveryAttempt()
    order_id = int(order.id)
    try:
        order = await fulfill_paid_order(session, order, attempt=attempt)
    except Exception as exc:  # every failure is recorded and rescheduled or given up below
        logger.warning("auto delivery retry failed order=%s", order_id, exc_info=True)
        await rollback_quiet(session)
        return await _after_failed_attempt(session, failure_id, order, attempt=attempt, error=str(exc), now=now)
    if not is_delivered(order):
        await _reschedule(session, failure_id, now + retry_delay(1))
        return None
    claimed = await _set_failure(session, failure_id, resolved_at=now, next_auto_retry_at=None)
    return RecoveryResult(int(order.id), Outcome.DELIVERED) if claimed else None


async def recover_one(
    session: AsyncSession, failure_id: int, *, now: datetime, panel_up: bool
) -> RecoveryResult | None:
    """One due row: retry, reschedule, refund or hand over. None when nobody needs a notice."""
    if not await _lease(session, failure_id, now):
        return None
    failure = await session.get(DeliveryFailure, failure_id, populate_existing=True)
    order = await session.get(Order, failure.order_id, populate_existing=True) if failure else None
    payment = await _approved_payment(session, order.id) if order is not None else None
    if failure is None or not _is_recoverable(order, payment):
        # Delivered, cancelled or not ours to repeat: staff got the stuck notice already.
        await _stop(session, failure_id, now)
        return None
    token = set_shop_reseller_id(int(order.reseller_id) if order.reseller_id else None)
    try:
        return await _recover_order(session, failure, order, now=now, panel_up=panel_up)
    finally:
        reset_shop_reseller_id(token)


async def _recover_order(
    session: AsyncSession, failure: DeliveryFailure, order: Order, *, now: datetime, panel_up: bool
) -> RecoveryResult | None:
    failure_id = int(failure.id)
    started = _aware(failure.created_at) or now
    if int(failure.auto_attempts or 0) >= MAX_AUTO_ATTEMPTS:
        return await _give_up(session, failure_id, order, now=now, reason=REASON_EXHAUSTED)
    if now - started >= RETRY_WINDOW:
        return await _give_up(session, failure_id, order, now=now, reason=REASON_WINDOW)
    if not panel_up:
        await _reschedule(session, failure_id, now + PANEL_DOWN_RECHECK)
        return None
    if order.status == OrderStatus.DELIVERING.value:
        if is_new_purchase_order(order):
            # A create that never finished may have reached the panel.
            await _stop(session, failure_id, now)
            return RecoveryResult(int(order.id), Outcome.STAFF, REASON_CREATE_UNKNOWN)
        if not await _reopen_stale_renewal(session, order, now):
            await _reschedule(session, failure_id, now + PANEL_DOWN_RECHECK)
            return None
    return await _attempt(session, failure_id, order, now=now)


def _aware(moment: datetime | None) -> datetime | None:
    # SQLite returns naive values for server-side timestamps; they are UTC.
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=timezone.utc)


async def _tell_buyer_delivered(session: AsyncSession, order: Order) -> None:
    from app.db.models import BotUser
    from app.services.delivery import send_delivery_to_user
    from app.services.reseller_bots import open_notify_bot_for_user

    if (order.note or "").endswith(":auto"):
        # Automatic renewals report their own result to the customer.
        return
    user = await session.get(BotUser, order.user_id)
    if user is None or not user.telegram_id:
        return
    payment = await _approved_payment(session, order.id)
    bot, should_close = await open_notify_bot_for_user(session, user, shop_id=order.reseller_id)
    try:
        await send_delivery_to_user(bot, user.telegram_id, session, payment, order)
    finally:
        if should_close:
            await bot.session.close()


async def _tell_staff(session: AsyncSession, bot: Bot, order: Order, result: RecoveryResult) -> None:
    from app.db.models import BotUser
    from app.services.notifications import notify_auto_recovery_closed, notify_new_subscription

    if result.outcome is Outcome.DELIVERED:
        user = await session.get(BotUser, order.user_id)
        plan = await session.get(Plan, order.plan_id) if order.plan_id else None
        await notify_new_subscription(
            bot, session, order=order, user_tg_id=getattr(user, "telegram_id", None),
            plan_name=plan.name if plan else None, after_retry=True,
        )
        return
    refunded = result.resolution.refunded if result.resolution is not None else None
    await notify_auto_recovery_closed(bot, session, order=order, refunded=refunded, reason=result.reason)


async def notify_recovery_result(session: AsyncSession, bot: Bot, result: RecoveryResult) -> None:
    """Tell the customer and the order's own staff (shop staff for a shop order) the outcome."""
    order = await session.get(Order, result.order_id, populate_existing=True)
    if order is None:
        return
    try:
        if result.outcome is Outcome.DELIVERED:
            await _tell_buyer_delivered(session, order)
        elif result.outcome is Outcome.REFUNDED and result.resolution is not None:
            await tell_customer_refunded(session, result.resolution)
    except NOTICE_ERRORS:
        # The order itself is settled; only the message was lost (logged for support).
        logger.warning("recovery customer notice failed order=%s", order.id, exc_info=True)
    try:
        await _tell_staff(session, bot, order, result)
    except NOTICE_ERRORS:
        logger.warning("recovery staff notice failed order=%s", order.id, exc_info=True)
