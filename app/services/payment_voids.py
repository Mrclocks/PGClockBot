"""Void a payment that was approved by mistake: stop what it bought and optionally return the money.

The payment is claimed as voided first and committed; only then is the panel touched. A panel
failure moves the void to ``review`` with the saved target, and a retry disables exactly the same
services. The wallet refund is paid in the same transaction that seals the void, so it runs once.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentVoid,
    ServiceAutomation,
    UserService,
)
from app.services.loyalty import reverse_order_points
from app.services.orders import _order_note, _wallet_topup_already_credited, is_mutation_order_note
from app.services.pasarguard import get_pg, get_pg_for_reseller, is_pg_not_found
from app.services.shop_scope import payment_shop_id
from app.services.wallet import credit_wallet, debit_wallet

logger = logging.getLogger(__name__)

VOID_REASON_MAX = 500
LEASE_MINUTES = 5
STATUS_PROCESSING = "processing"
STATUS_REVIEW = "review"
STATUS_DONE = "done"
OPEN_VOID_STATUSES = (STATUS_PROCESSING, STATUS_REVIEW)
# Only payments a person approved by hand can be wrong; a verified gateway payment cannot.
VOIDABLE_METHODS = frozenset({PaymentMethod.CARD.value, PaymentMethod.CRYPTO.value})
VOIDABLE_ORDER_STATUSES = (OrderStatus.DELIVERED.value, OrderStatus.PAID.value)
STATUS_LABELS = {
    STATUS_PROCESSING: "در حال ابطال",
    STATUS_REVIEW: "نیازمند بررسی و تلاش مجدد",
    STATUS_DONE: "ابطال شد",
}
NOT_FOUND = "پرداخت یافت نشد"


@dataclass(slots=True)
class VoidPlan:
    """What a void will do, resolved from the payment before anything is changed."""

    payment: Payment
    order: Order | None
    service_ids: list[int]
    refund_amount: int


def _clean_reason(reason: str) -> str:
    text = " ".join((reason or "").split())
    if not text:
        raise ValueError("دلیل ابطال را بنویسید")
    if len(text) > VOID_REASON_MAX:
        raise ValueError("دلیل ابطال طولانی است")
    return text


async def _order_service_ids(session: AsyncSession, order: Order) -> list[int]:
    """Every service minted for the order; a wholesale order has several sharing one remark."""
    rows = await session.scalars(
        select(UserService.id)
        .where(
            UserService.bot_user_id == order.user_id,
            or_(UserService.id == order.service_id, UserService.remark == f"order:{order.id}"),
        )
        .order_by(UserService.id)
    )
    return [int(service_id) for service_id in rows]


async def _load_payment(session: AsyncSession, payment_id: int, shop_id: int | None) -> tuple[Payment, Order | None]:
    payment = await session.get(Payment, int(payment_id))
    order = await session.get(Order, payment.order_id) if payment and payment.order_id else None
    # The shop comes from the loaded record; a payment of another shop reads as missing.
    if payment is None or (order is None and not payment.is_wallet_topup):
        raise ValueError(NOT_FOUND)
    if payment_shop_id(payment, order) != shop_id:
        raise ValueError(NOT_FOUND)
    return payment, order


async def _resolve_plan(session: AsyncSession, payment: Payment, order: Order | None, refund_to_wallet: bool) -> VoidPlan:
    if payment.method not in VOIDABLE_METHODS:
        raise ValueError("ابطال فقط برای پرداخت‌های بررسی‌دستی (کارت یا ارز دیجیتال) ممکن است")
    if payment.is_wallet_topup:
        if refund_to_wallet:
            raise ValueError("برای شارژ کیف پول، مبلغ از کیف مشتری کم می‌شود؛ بازگشت وجه معنا ندارد")
        if not await _wallet_topup_already_credited(session, payment):
            raise ValueError("اعتبار این شارژ هرگز واریز نشده بود؛ ابطال ممکن نیست")
        return VoidPlan(payment, None, [], 0)
    if is_mutation_order_note(_order_note(order)):
        raise ValueError("ابطال پرداخت تمدید، افزونه یا نمایندگی از این بخش ممکن نیست؛ سرویس را دستی ویرایش کنید")
    if order.status not in VOIDABLE_ORDER_STATUSES:
        raise ValueError("سفارش این پرداخت در وضعیتی نیست که بتوان ابطال کرد")
    service_ids = await _order_service_ids(session, order)
    if order.status == OrderStatus.DELIVERED.value and not service_ids:
        raise ValueError("سرویس تحویل‌شدهٔ این سفارش پیدا نشد؛ ابتدا سفارش را بررسی کنید")
    return VoidPlan(payment, order, service_ids, int(payment.amount) if refund_to_wallet else 0)


async def _find_void(session: AsyncSession, payment_id: int) -> PaymentVoid | None:
    return await session.scalar(select(PaymentVoid).where(PaymentVoid.payment_id == payment_id))


async def _take_lease(session: AsyncSession, void_id: int) -> str | None:
    """Claim the void for this run; None when another run holds a live lease or it is done."""
    now = datetime.now(timezone.utc)
    token = secrets.token_hex(16)
    claim = await session.execute(
        update(PaymentVoid)
        .where(
            PaymentVoid.id == void_id,
            PaymentVoid.status.in_(OPEN_VOID_STATUSES),
            or_(PaymentVoid.locked_until.is_(None), PaymentVoid.locked_until < now),
        )
        .values(status=STATUS_PROCESSING, lock_token=token, locked_until=now + timedelta(minutes=LEASE_MINUTES))
        .execution_options(synchronize_session=False)
    )
    return token if claim.rowcount == 1 else None


async def _claim_void(
    session: AsyncSession, plan: VoidPlan, *, shop_id: int | None, actor: str, reason: str, status: str, token: str | None
) -> PaymentVoid:
    """Insert the void and flip the payment to voided in the caller's open transaction."""
    payment = plan.payment
    row = PaymentVoid(
        payment_id=payment.id,
        shop_id=shop_id,
        actor=actor,
        reason=reason,
        refund_amount=plan.refund_amount,
        status=status,
        service_ids=json.dumps(plan.service_ids),
        lock_token=token,
        locked_until=datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES) if token else None,
        completed_at=datetime.now(timezone.utc) if status == STATUS_DONE else None,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # A second click already created the void for this payment.
        await session.rollback()
        raise ValueError("ابطال این پرداخت در حال انجام یا انجام‌شده است") from None
    claim = await session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.status == PaymentStatus.APPROVED.value)
        .values(status=PaymentStatus.VOIDED.value, review_note=f"voided: {reason}"[:1000])
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        await session.rollback()
        raise ValueError("فقط پرداخت تأییدشده قابل ابطال است")
    return row


async def _void_topup(session: AsyncSession, plan: VoidPlan, *, actor: str, reason: str) -> PaymentVoid:
    """A top-up needs no panel call, so the claim, the debit and the seal commit together."""
    payment = plan.payment
    row = await _claim_void(
        session, plan, shop_id=payment.wallet_shop_id, actor=actor, reason=reason, status=STATUS_DONE, token=None
    )
    user = await session.get(BotUser, payment.user_id)
    try:
        await debit_wallet(
            session, user, int(payment.amount), f"ابطال شارژ کیف پول #{payment.id}",
            shop_id=payment.wallet_shop_id, commit=False,
        )
    except ValueError:
        await session.rollback()
        raise ValueError("موجودی کیف پول مشتری کمتر از مبلغ شارژ است؛ پیش از ابطال، اعتبار را دستی تعدیل کنید") from None
    await session.commit()
    return row


async def _disable_panel_services(session: AsyncSession, void: PaymentVoid, order: Order) -> None:
    """Disable every saved target; disabling is idempotent, so a retry repeats it safely."""
    service_ids = json.loads(void.service_ids)
    if not service_ids:
        return
    pg = await get_pg_for_reseller(session, void.shop_id) if void.shop_id else get_pg()
    services = list(await session.scalars(select(UserService).where(UserService.id.in_(service_ids))))
    for service in services:
        if service.bot_user_id != order.user_id or not service.pg_user_id:
            raise ValueError("مالکیت یا شناسهٔ پنل سرویس تغییر کرده است")
        try:
            info = await pg.get_user_by_id(service.pg_user_id)
            if int(info.get("id") or 0) != service.pg_user_id:
                raise ValueError("پاسخ پنل با سرویس همخوان نیست")
            if str(info.get("status") or "").lower() != "disabled":
                await pg.set_disabled_by_id(service.pg_user_id, True)
                info = await pg.get_user_by_id(service.pg_user_id)
                if str(info.get("status") or "").lower() != "disabled":
                    raise ValueError("غیرفعال‌شدن سرویس در پنل تأیید نشد")
        except Exception as exc:
            if not is_pg_not_found(exc):
                raise


async def _seal(session: AsyncSession, void_id: int, token: str) -> None:
    sealed = await session.execute(
        update(PaymentVoid)
        .where(PaymentVoid.id == void_id, PaymentVoid.lock_token == token, PaymentVoid.status == STATUS_PROCESSING)
        .values(status=STATUS_DONE, lock_token=None, locked_until=None, completed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if sealed.rowcount != 1:
        raise ValueError("نتیجه نیاز به بررسی اپراتور دارد")


async def _release_to_review(session: AsyncSession, void_id: int, token: str) -> None:
    await session.execute(
        update(PaymentVoid)
        .where(PaymentVoid.id == void_id, PaymentVoid.lock_token == token)
        .values(status=STATUS_REVIEW, lock_token=None, locked_until=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _finish_order_void(session: AsyncSession, void: PaymentVoid, token: str, order: Order) -> None:
    """Seal the void, stop the services locally, cancel the order and pay the refund in one commit."""
    await _seal(session, void.id, token)
    service_ids = json.loads(void.service_ids)
    if service_ids:
        await session.execute(
            update(UserService)
            .where(UserService.id.in_(service_ids))
            .values(is_cancelled=True, cancellation_pending=False)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(ServiceAutomation)
            .where(ServiceAutomation.service_id.in_(service_ids))
            .values(renew_enabled=False, duration_enabled=False, volume_enabled=False)
            .execution_options(synchronize_session=False)
        )
    cancelled = await session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status.in_(VOIDABLE_ORDER_STATUSES))
        .values(status=OrderStatus.CANCELLED.value)
        .execution_options(synchronize_session=False)
    )
    if cancelled.rowcount != 1:
        raise ValueError("وضعیت سفارش هم‌زمان تغییر کرد")
    if void.refund_amount:
        user = await session.get(BotUser, order.user_id)
        await credit_wallet(
            session, user, int(void.refund_amount), f"بازگشت وجه ابطال پرداخت #{void.payment_id}",
            shop_id=void.shop_id, commit=False,
        )
    await session.commit()


async def _run_order_void(session: AsyncSession, void: PaymentVoid, token: str, order: Order) -> None:
    void_id = int(void.id)  # a rollback expires the row, so read the id while it is loaded
    try:
        await _disable_panel_services(session, void, order)
        await _finish_order_void(session, void, token, order)
    except Exception as exc:
        # The panel call may have succeeded, so keep the saved target and let the operator retry.
        await session.rollback()
        logger.warning("payment void %s needs review", void_id, exc_info=True)
        await _release_to_review(session, void_id, token)
        raise ValueError(
            "نتیجهٔ پنل نامشخص است؛ پرداخت ابطال‌شده علامت خورد و مبلغی برنگشته است. دوباره تلاش کنید."
        ) from exc


async def _reverse_points(session: AsyncSession, order: Order | None, payment_id: int) -> None:
    """Loyalty points commit on their own, so they run after the void is sealed."""
    if order is None:
        return
    try:
        await reverse_order_points(session, order, reason=f"ابطال پرداخت #{payment_id}")
    except Exception:
        logger.warning("loyalty reversal failed for voided payment %s", payment_id, exc_info=True)
        await session.rollback()


async def void_approved_payment(
    session: AsyncSession,
    payment_id: int,
    *,
    shop_id: int | None,
    actor: str,
    reason: str,
    refund_to_wallet: bool = False,
) -> PaymentVoid:
    """Void an approved payment of ``shop_id`` (None = platform). Safe to call again after a failure."""
    payment, order = await _load_payment(session, payment_id, shop_id)
    existing = await _find_void(session, payment.id)
    if existing is not None:
        return await _resume_void(session, existing, order, refund_to_wallet)
    reason = _clean_reason(reason)
    plan = await _resolve_plan(session, payment, order, refund_to_wallet)
    if payment.is_wallet_topup:
        return await _void_topup(session, plan, actor=actor[:128], reason=reason)
    token = secrets.token_hex(16)
    void = await _claim_void(
        session, plan, shop_id=shop_id, actor=actor[:128], reason=reason, status=STATUS_PROCESSING, token=token
    )
    await session.commit()
    await _run_order_void(session, void, token, order)
    await _reverse_points(session, order, payment.id)
    await session.refresh(void)
    return void


async def _resume_void(
    session: AsyncSession, void: PaymentVoid, order: Order | None, refund_to_wallet: bool
) -> PaymentVoid:
    """Retry a void left in review, reusing the target and refund choice saved by the first run."""
    if void.status == STATUS_DONE:
        return void
    if bool(void.refund_amount) != bool(refund_to_wallet):
        raise ValueError("در تلاش مجدد، انتخاب بازگشت وجه قابل تغییر نیست")
    token = await _take_lease(session, void.id)
    if token is None:
        await session.rollback()
        raise ValueError("این ابطال در حال پردازش است")
    await session.commit()
    await _run_order_void(session, void, token, order)
    await _reverse_points(session, order, void.payment_id)
    await session.refresh(void)
    return void


async def list_voids(session: AsyncSession, payment_ids: list[int]) -> dict[int, PaymentVoid]:
    """Void rows of the payments on one page, keyed by payment id."""
    if not payment_ids:
        return {}
    rows = await session.scalars(select(PaymentVoid).where(PaymentVoid.payment_id.in_(payment_ids)))
    return {int(row.payment_id): row for row in rows}
