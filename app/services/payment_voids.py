"""Void a payment that was approved by mistake: stop what it bought and optionally return the money.

A new service is disabled. A renewal or add-on is taken back instead: the service's quota and plan
from just before it, saved on the order, are written back, as long as the panel still shows what
the renewal or add-on wrote.

The payment is claimed as voided first and committed; only then is the panel touched. A panel
failure moves the void to ``review`` with the saved target, and a retry repeats exactly the same
change. The wallet refund is paid in the same transaction that seals the void, so it runs once.
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
    Plan,
    ServiceAutomation,
    UserService,
)
from app.services.bot_user_admin import sync_service_quota_cache
from app.services.formatting import format_toman
from app.services.cashback import reverse_after_refund
from app.services.loyalty import reverse_order_points
from app.services.orders import _order_note, _wallet_topup_already_credited, is_addon_order_note, is_mutation_order_note
from app.services.pasarguard import get_pg, get_pg_for_reseller, is_pg_not_found
from app.services.payer_notify import notify_payer
from app.services.support_button import support_markup
from app.services.service_cancellations import lock_service_mutation
from app.services.service_restore import SavedChange, assert_still_changed, put_back, saved_change
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
# Fixed reasons for the bot, which has no free-text step here; the key fits a callback payload.
BOT_REASONS = {
    "1": "رسید جعلی یا نامعتبر",
    "2": "مبلغ پرداخت اشتباه بود",
    "3": "پرداخت تکراری",
    "4": "پرداخت به‌اشتباه تأیید شد",
}
CANDIDATE_LIMIT = 10


@dataclass(slots=True)
class VoidPlan:
    """What a void will do, resolved from the payment before anything is changed."""

    payment: Payment
    order: Order | None
    service_ids: list[int]
    refund_amount: int
    # Set for a renewal or add-on: the quota to write back instead of disabling the service.
    change: SavedChange | None = None


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
        return _extension_plan(payment, order, refund_to_wallet)
    if order.status not in VOIDABLE_ORDER_STATUSES:
        raise ValueError("سفارش این پرداخت در وضعیتی نیست که بتوان ابطال کرد")
    service_ids = await _order_service_ids(session, order)
    if order.status == OrderStatus.DELIVERED.value and not service_ids:
        raise ValueError("سرویس تحویل‌شدهٔ این سفارش پیدا نشد؛ ابتدا سفارش را بررسی کنید")
    return VoidPlan(payment, order, service_ids, int(payment.amount) if refund_to_wallet else 0)


def is_extension_order(order: Order | None) -> bool:
    """A renewal or add-on: it changed a service the customer already had."""
    note = _order_note(order)
    return note.startswith("renew:") or is_addon_order_note(note)


def _extension_change(order: Order) -> SavedChange | None:
    change = saved_change(order.service_restore)
    return change if change is not None and change.service_id == order.service_id else None


def _extension_plan(payment: Payment, order: Order, refund_to_wallet: bool) -> VoidPlan:
    if not is_extension_order(order):
        raise ValueError("ابطال پرداخت نمایندگی از این بخش ممکن نیست")
    if order.status != OrderStatus.DELIVERED.value:
        raise ValueError("سفارش این پرداخت در وضعیتی نیست که بتوان ابطال کرد")
    change = _extension_change(order)
    if change is None:
        raise ValueError("وضعیت سرویس پیش از این تمدید یا افزونه ثبت نشده است؛ سرویس را دستی ویرایش کنید")
    return VoidPlan(payment, order, [change.service_id], int(payment.amount) if refund_to_wallet else 0, change)


async def _extended_service(session: AsyncSession, order: Order, change: SavedChange) -> UserService:
    service = await session.get(UserService, change.service_id)
    if service is None or service.bot_user_id != order.user_id or not service.pg_user_id:
        raise ValueError("مالکیت یا شناسهٔ پنل سرویس تغییر کرده است")
    return service


async def _panel_for(session: AsyncSession, shop_id: int | None):
    return await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()


async def _check_extension(session: AsyncSession, plan: VoidPlan, shop_id: int | None) -> None:
    """Refuse before anything is claimed when the service changed again since the renewal or add-on."""
    service = await _extended_service(session, plan.order, plan.change)
    pg_user_id = int(service.pg_user_id)
    pg = await _panel_for(session, shop_id)
    # Nothing is written yet; end the read transaction before the panel call.
    await session.commit()
    await assert_still_changed(pg, pg_user_id, plan.change)


async def _lock_extended_service(session: AsyncSession, order: Order) -> None:
    """Hold the service's one-change-at-a-time lock until the void is sealed or reviewed."""
    try:
        await lock_service_mutation(session, order.service_id)
        locked = await session.execute(
            update(Order)
            .where(Order.id == order.id, Order.status == OrderStatus.DELIVERED.value)
            .values(service_mutation_pending=True)
            .execution_options(synchronize_session=False)
        )
    except IntegrityError:
        await session.rollback()
        raise ValueError("تغییر دیگری برای این سرویس در حال انجام یا بررسی است") from None
    except ValueError:
        await session.rollback()
        raise
    if locked.rowcount != 1:
        await session.rollback()
        raise ValueError("وضعیت سفارش هم‌زمان تغییر کرد")


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
    pg = await _panel_for(session, void.shop_id)
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


async def _put_back_panel_quota(session: AsyncSession, void: PaymentVoid, order: Order, change: SavedChange) -> None:
    service = await _extended_service(session, order, change)
    pg = await _panel_for(session, void.shop_id)
    try:
        await put_back(pg, int(service.pg_user_id), change)
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


async def _stop_local_services(session: AsyncSession, void: PaymentVoid) -> None:
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


async def _restore_local_service(session: AsyncSession, order: Order, change: SavedChange) -> None:
    """Mirror the written-back quota on the service row, and its plan when that plan still exists."""
    service = await _extended_service(session, order, change)
    if change.plan_id and await session.get(Plan, change.plan_id) is not None:
        service.plan_id = change.plan_id
    sync_service_quota_cache(service, change.before.payload())


async def _finish_order_void(
    session: AsyncSession, void: PaymentVoid, token: str, order: Order, change: SavedChange | None
) -> None:
    """Seal the void, settle the service locally, cancel the order and pay the refund in one commit."""
    await _seal(session, void.id, token)
    if change is None:
        await _stop_local_services(session, void)
    else:
        await _restore_local_service(session, order, change)
    cancelled = await session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status.in_(VOIDABLE_ORDER_STATUSES))
        .values(status=OrderStatus.CANCELLED.value, service_mutation_pending=False)
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
    change = _extension_change(order) if is_extension_order(order) else None
    try:
        if change is None:
            await _disable_panel_services(session, void, order)
        else:
            await _put_back_panel_quota(session, void, order, change)
        await _finish_order_void(session, void, token, order, change)
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
    await reverse_after_refund(session, order_id=int(order.id), reason=f"ابطال پرداخت #{payment_id}")


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
    if plan.change is not None:
        await _check_extension(session, plan, shop_id)
    token = secrets.token_hex(16)
    void = await _claim_void(
        session, plan, shop_id=shop_id, actor=actor[:128], reason=reason, status=STATUS_PROCESSING, token=token
    )
    if plan.change is not None:
        await _lock_extended_service(session, order)
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


def offers_void(payment: Payment, order: Order | None) -> bool:
    """True when the bot should offer «ابطال» for this payment (the service re-checks on submit)."""
    if payment.status != PaymentStatus.APPROVED.value or payment.method not in VOIDABLE_METHODS:
        return False
    if payment.is_wallet_topup or order is None:
        return False
    if is_mutation_order_note(_order_note(order)):
        return is_extension_order(order) and order.status == OrderStatus.DELIVERED.value and _extension_change(order) is not None
    return order.status in VOIDABLE_ORDER_STATUSES


async def list_void_candidates(
    session: AsyncSession, *, shop_id: int | None, limit: int = CANDIDATE_LIMIT
) -> list[tuple[Payment, Order, BotUser | None, PaymentVoid | None]]:
    """Recent voidable purchase payments of one shop, plus voids waiting for a retry."""
    shop_clause = Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)
    rows = await session.execute(
        select(Payment, Order, BotUser, PaymentVoid)
        .join(Order, Order.id == Payment.order_id)
        .outerjoin(BotUser, BotUser.id == Payment.user_id)
        .outerjoin(PaymentVoid, PaymentVoid.payment_id == Payment.id)
        .where(
            shop_clause,
            Payment.is_wallet_topup.is_(False),
            Payment.method.in_(sorted(VOIDABLE_METHODS)),
            or_(
                Payment.status == PaymentStatus.APPROVED.value,
                PaymentVoid.status.in_(OPEN_VOID_STATUSES),
            ),
        )
        .order_by(Payment.id.desc())
        .limit(max(1, min(int(limit), 50)))
    )
    return [
        (payment, order, user, void)
        for payment, order, user, void in rows.all()
        if void is not None or offers_void(payment, order)
    ]


def void_effect(order: Order | None) -> str:
    """What a void of this order does to the service, for the confirmation step."""
    if is_extension_order(order):
        return "حجم، زمان و پلن سرویس به پیش از این تمدید یا افزونه برمی‌گردد و سفارش لغو می‌شود."
    return "سرویس این سفارش در پنل غیرفعال و سفارش لغو می‌شود."


def void_customer_notice(payment: Payment, refund_amount: int, order: Order | None = None) -> str:
    if payment.is_wallet_topup:
        return f"شارژ کیف پول #{payment.id} ابطال شد و مبلغ آن از کیف پول شما کم شد. اگر اشتباهی رخ داده با پشتیبانی تماس بگیرید."
    if is_extension_order(order):
        text = f"پرداخت #{payment.id} ابطال شد و تمدید یا افزونهٔ آن از سرویس برداشته شد."
    else:
        text = f"پرداخت #{payment.id} ابطال و سرویس مربوط به آن غیرفعال شد."
    if refund_amount:
        text += f" مبلغ {format_toman(refund_amount)} به کیف پول شما برگشت."
    return text + " اگر اشتباهی رخ داده با پشتیبانی تماس بگیرید."


async def void_and_notify(
    session: AsyncSession,
    payment_id: int,
    *,
    shop_id: int | None,
    actor: str,
    reason: str,
    refund_to_wallet: bool = False,
) -> PaymentVoid:
    """Void the payment, then tell the customer once (not again when the void was already done)."""
    previous = (await list_voids(session, [int(payment_id)])).get(int(payment_id))
    void = await void_approved_payment(
        session, payment_id, shop_id=shop_id, actor=actor, reason=reason, refund_to_wallet=refund_to_wallet
    )
    if previous is None or previous.status != STATUS_DONE:
        payment = await session.get(Payment, int(payment_id))
        order = await session.get(Order, payment.order_id) if payment.order_id else None
        await notify_payer(
            session, payment.user_id, title="⚠️ پرداخت ابطال شد",
            body=void_customer_notice(payment, int(void.refund_amount), order), shop_id=void.shop_id,
            reply_markup=support_markup(),
        )
    return void


async def load_void_candidate(
    session: AsyncSession, payment_id: int, *, shop_id: int | None
) -> tuple[Payment, Order | None, PaymentVoid | None]:
    """One payment of ``shop_id`` with its void row; a payment of another shop reads as missing."""
    payment, order = await _load_payment(session, payment_id, shop_id)
    return payment, order, await _find_void(session, payment.id)
