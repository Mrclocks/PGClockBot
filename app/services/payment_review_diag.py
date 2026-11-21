"""Payment-review diagnosis — shared by bot payrev and web finance delivery.

Phase 0: classify state precisely so admins see the real situation
(approve vs resume fulfill vs Telegram resend vs truly complete)
instead of a misleading «قبلاً تأیید شده».

Phase 1: map each kind to exactly one preferred action verb:
  ``approve`` (payrev:ok) / ``resume`` (payrev:go) / ``resend`` (payrev:send).

Security invariants (read-only classifier; callers decide actions):
- Never invent a second approve claim.
- ``approved_resend`` means service/credit already exists — only Telegram
  re-send is safe (no mint / no wallet credit).
- ``approved_fulfill`` means APPROVED but order work incomplete — resume
  via ``approve_payment`` / fulfill is the safe path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DeliveryFailure,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
)
from app.services.orders import (
    _wallet_topup_already_credited,
    is_mutation_order_note,
)
from app.services import bot_feedback_texts as feedback

logger = logging.getLogger(__name__)

PaymentReviewKind = Literal[
    "missing",
    "pending",
    "rejected",
    "approved_wallet_resume",
    "approved_wallet_done",
    "approved_orphan",
    "approved_fulfill",
    "approved_resend",
    "approved_complete",
    "unapprovable",
]

# Phase 1 action verbs — one preferred tool per diagnosis.
PaymentReviewAction = Literal["approve", "resume", "resend", "none"]

# Callback path segment after ``payrev:`` for each action.
ACTION_CALLBACK_VERB: dict[PaymentReviewAction, str] = {
    "approve": "ok",
    "resume": "go",
    "resend": "send",
    "none": "",
}

ACTION_LABEL_FA: dict[PaymentReviewAction, str] = {
    "approve": "تأیید پرداخت",
    "resume": "ادامه تحویل",
    "resend": "ارسال مجدد پیام",
    "none": "—",
}

ACTION_BTN_FA: dict[PaymentReviewAction, str] = {
    "approve": "🟢✅ تأیید",
    "resume": "♻️ ادامه تحویل",
    "resend": "📤 ارسال مجدد پیام",
    "none": "—",
}


@dataclass(frozen=True)
class PaymentReviewDiagnosis:
    """Immutable snapshot of what payrev / delivery UI should communicate."""

    kind: PaymentReviewKind
    payment_id: int | None
    payment_status: str | None
    order_id: int | None
    order_status: str | None
    has_service: bool
    is_wallet_topup: bool
    has_open_delivery_failure: bool
    wallet_credited: bool | None
    can_claim_approve: bool
    can_resume_fulfill: bool
    can_resend_telegram: bool
    label_fa: str
    alert_fa: str
    detail_fa: str
    log_code: str

    @property
    def preferred_action(self) -> PaymentReviewAction:
        if self.can_claim_approve:
            return "approve"
        if self.can_resume_fulfill:
            return "resume"
        if self.can_resend_telegram:
            return "resend"
        return "none"

    @property
    def action_label_fa(self) -> str:
        return ACTION_LABEL_FA[self.preferred_action]

    @property
    def action_btn_fa(self) -> str:
        return ACTION_BTN_FA[self.preferred_action]

    @property
    def callback_verb(self) -> str:
        return ACTION_CALLBACK_VERB[self.preferred_action]

    def callback_data(self, payment_id: int | None = None) -> str | None:
        """``payrev:{ok|go|send}:{id}`` or None when no action."""
        verb = self.callback_verb
        pid = payment_id if payment_id is not None else self.payment_id
        if not verb or pid is None:
            return None
        return f"payrev:{verb}:{int(pid)}"

    def allows_action(self, action: PaymentReviewAction) -> bool:
        if action == "approve":
            return self.can_claim_approve
        if action == "resume":
            return self.can_resume_fulfill
        if action == "resend":
            return self.can_resend_telegram
        return False

    def wrong_tool_alert(self, attempted: PaymentReviewAction) -> str:
        """Alert when admin presses a button that does not match diagnosis."""
        if self.preferred_action == "none":
            return self.alert_fa
        if attempted == self.preferred_action:
            return self.alert_fa
        wanted = self.action_label_fa
        return (
            f"این دکمه برای «{ACTION_LABEL_FA[attempted]}» است؛ "
            f"وضعیت فعلی نیاز به «{wanted}» دارد."
        )

    def as_log_extra(self) -> dict:
        return {
            "payrev_kind": self.kind,
            "payrev_code": self.log_code,
            "payrev_action": self.preferred_action,
            "payment_id": self.payment_id,
            "payment_status": self.payment_status,
            "order_id": self.order_id,
            "order_status": self.order_status,
            "has_service": self.has_service,
            "is_wallet_topup": self.is_wallet_topup,
            "open_delivery_failure": self.has_open_delivery_failure,
            "wallet_credited": self.wallet_credited,
        }


def _base(
    *,
    kind: PaymentReviewKind,
    payment: Payment | None,
    order: Order | None,
    has_open_delivery_failure: bool = False,
    wallet_credited: bool | None = None,
    can_claim_approve: bool = False,
    can_resume_fulfill: bool = False,
    can_resend_telegram: bool = False,
    label_fa: str,
    alert_fa: str,
    detail_fa: str,
    log_code: str,
) -> PaymentReviewDiagnosis:
    has_service = bool(order and order.service_id)
    return PaymentReviewDiagnosis(
        kind=kind,
        payment_id=int(payment.id) if payment is not None else None,
        payment_status=str(payment.status) if payment is not None else None,
        order_id=int(order.id) if order is not None else (
            int(payment.order_id) if payment is not None and payment.order_id else None
        ),
        order_status=str(order.status) if order is not None else None,
        has_service=has_service,
        is_wallet_topup=bool(payment and payment.is_wallet_topup),
        has_open_delivery_failure=bool(has_open_delivery_failure),
        wallet_credited=wallet_credited,
        can_claim_approve=can_claim_approve,
        can_resume_fulfill=can_resume_fulfill,
        can_resend_telegram=can_resend_telegram,
        label_fa=label_fa,
        alert_fa=alert_fa,
        detail_fa=detail_fa,
        log_code=log_code,
    )


@dataclass(frozen=True, slots=True)
class OpenFailureKeys:
    """Orders and payments that have an unresolved delivery failure, read in one query."""

    order_ids: frozenset[int]
    payment_ids: frozenset[int]

    def has(self, *, order_id: int | None, payment_id: int | None) -> bool:
        return bool(order_id and order_id in self.order_ids) or bool(payment_id and payment_id in self.payment_ids)


async def load_open_failure_keys(
    session: AsyncSession, *, order_ids: Iterable[int], payment_ids: Iterable[int]
) -> OpenFailureKeys:
    orders, payments = {int(i) for i in order_ids}, {int(i) for i in payment_ids}
    if not orders and not payments:
        return OpenFailureKeys(frozenset(), frozenset())
    rows = await session.execute(
        select(DeliveryFailure.order_id, DeliveryFailure.payment_id).where(
            DeliveryFailure.resolved_at.is_(None),
            or_(DeliveryFailure.order_id.in_(orders), DeliveryFailure.payment_id.in_(payments)),
        )
    )
    found = rows.all()
    return OpenFailureKeys(
        frozenset(int(o) for o, _ in found if o is not None and int(o) in orders),
        frozenset(int(p) for _, p in found if p is not None and int(p) in payments),
    )


async def _has_open_delivery_failure(
    session: AsyncSession,
    *,
    order_id: int | None,
    payment_id: int | None,
    known: OpenFailureKeys | None = None,
) -> bool:
    if known is None:
        known = await load_open_failure_keys(
            session, order_ids=[order_id] if order_id else [], payment_ids=[payment_id] if payment_id else []
        )
    return known.has(order_id=order_id, payment_id=payment_id)


def _order_work_complete(order: Order) -> bool:
    """True when fulfill must not re-run (mirrors ``_resume_approved_payment``)."""
    note = (order.note or "").strip()
    is_mutation = is_mutation_order_note(note)
    if order.status == OrderStatus.DELIVERED.value:
        return True
    if bool(order.service_id) and not is_mutation:
        return True
    return False


def _note_hints_send_fail(payment: Payment | None) -> bool:
    note = ((payment.review_note if payment else None) or "").strip().lower()
    return "delivery_failed" in note or "send" in note and "fail" in note


async def diagnose_payment_review(
    session: AsyncSession,
    payment: Payment | None,
    *,
    order: Order | None = None,
    open_failures: OpenFailureKeys | None = None,
) -> PaymentReviewDiagnosis:
    """Classify a payment for review / stuck-delivery UX (bot + web).

    ``open_failures`` is the result of ``load_open_failure_keys`` for a page of payments; without it
    the open failure of this one payment is looked up.
    """
    if payment is None:
        return _base(
            kind="missing",
            payment=None,
            order=order,
            label_fa=feedback.NOT_FOUND,
            alert_fa="پرداخت یافت نشد",
            detail_fa="ردیف پرداخت در دیتابیس نیست.",
            log_code="payrev.missing",
        )

    status = str(payment.status or "")

    if status == PaymentStatus.PENDING.value:
        return _base(
            kind="pending",
            payment=payment,
            order=order,
            can_claim_approve=True,
            label_fa="در انتظار تأیید",
            alert_fa="پرداخت در انتظار تأیید است",
            detail_fa="هنوز تأیید نشده — با تأیید، شارژ/تحویل انجام می‌شود.",
            log_code="payrev.pending",
        )

    if status == PaymentStatus.REJECTED.value:
        return _base(
            kind="rejected",
            payment=payment,
            order=order,
            label_fa="رد شده",
            alert_fa="این پرداخت رد شده و قابل تأیید نیست",
            detail_fa="وضعیت رد شده است؛ برای ادامه باید پرداخت جدید ثبت شود.",
            log_code="payrev.rejected",
        )

    if status != PaymentStatus.APPROVED.value:
        return _base(
            kind="unapprovable",
            payment=payment,
            order=order,
            label_fa="وضعیت نامعتبر",
            alert_fa="این پرداخت قابل تأیید نیست",
            detail_fa=f"وضعیت پرداخت غیرمنتظره: {status or '—'}",
            log_code="payrev.unapprovable",
        )

    # --- APPROVED ---
    if payment.is_wallet_topup:
        credited = await _wallet_topup_already_credited(session, payment)
        if credited:
            return _base(
                kind="approved_wallet_done",
                payment=payment,
                order=None,
                wallet_credited=True,
                label_fa="شارژ انجام‌شده",
                alert_fa="شارژ کیف پول قبلاً انجام شده است",
                detail_fa="پرداخت تأیید و موجودی کیف ثبت شده — اقدام دوباره‌ای لازم نیست.",
                log_code="payrev.wallet_done",
            )
        return _base(
            kind="approved_wallet_resume",
            payment=payment,
            order=None,
            wallet_credited=False,
            can_resume_fulfill=True,
            label_fa="شارژ ناقص",
            alert_fa="پرداخت تأیید شده ولی شارژ کیف کامل نشده — ادامه می‌یابد",
            detail_fa="APPROVED بدون ردیف دفتر کل شارژ؛ ادامهٔ امن بدون دوبار شارژ.",
            log_code="payrev.wallet_resume",
        )

    if order is None and payment.order_id:
        order = await session.get(Order, int(payment.order_id))

    has_fail = await _has_open_delivery_failure(
        session,
        order_id=int(order.id) if order else (int(payment.order_id) if payment.order_id else None),
        payment_id=int(payment.id),
        known=open_failures,
    ) or _note_hints_send_fail(payment)

    if order is None:
        return _base(
            kind="approved_orphan",
            payment=payment,
            order=None,
            has_open_delivery_failure=has_fail,
            label_fa="سفارش مفقود",
            alert_fa="پرداخت تأیید شده ولی سفارش مرتبط یافت نشد",
            detail_fa="APPROVED بدون سفارش — نیاز به بررسی دستی دارد؛ تأیید دوباره ممکن نیست.",
            log_code="payrev.orphan",
        )

    if _order_work_complete(order):
        if has_fail:
            return _base(
                kind="approved_resend",
                payment=payment,
                order=order,
                has_open_delivery_failure=True,
                can_resend_telegram=True,
                label_fa="ارسال تلگرام ناموفق",
                alert_fa=(
                    "پرداخت تأیید و سرویس آماده است؛ ارسال تلگرام ناموفق بود — "
                    "فقط پیام تحویل دوباره فرستاده می‌شود"
                ),
                detail_fa=(
                    "سرویس/تحویل داخلی تمام است ولی ارسال به کاربر یا صف تحویل ناموفق باز است. "
                    "دوباره‌تأیید یا ساخت سرویس جدید انجام نمی‌شود."
                ),
                log_code="payrev.resend",
            )
        return _base(
            kind="approved_complete",
            payment=payment,
            order=order,
            has_open_delivery_failure=False,
            label_fa="تحویل‌شده",
            alert_fa="این پرداخت قبلاً تأیید و تحویل شده است",
            detail_fa="پرداخت APPROVED و سفارش DELIVERED/دارای سرویس — کار تمام است.",
            log_code="payrev.complete",
        )

    # Incomplete order work — safe to resume fulfill via approve_payment.
    return _base(
        kind="approved_fulfill",
        payment=payment,
        order=order,
        has_open_delivery_failure=has_fail,
        can_resume_fulfill=True,
        label_fa="تحویل ناقص",
        alert_fa="پرداخت تأیید شده ولی تحویل سرویس کامل نشده — ادامه می‌یابد",
        detail_fa=(
            f"سفارش در وضعیت «{order.status}» است و کار تحویل تمام نیست؛ "
            "ادامهٔ امن بدون ساخت سرویس تکراری."
        ),
        log_code="payrev.fulfill",
    )


async def diagnose_order_delivery(
    session: AsyncSession,
    order: Order,
    *,
    payment: Payment | None = None,
    payment_loaded: bool = False,
    open_failures: OpenFailureKeys | None = None,
) -> PaymentReviewDiagnosis:
    """Diagnose a stuck/failed delivery row (web finance tab).

    ``payment_loaded`` says the caller already looked the payment up, so ``None`` means there is none.
    """
    if payment is None and not payment_loaded:
        payment = (
            await session.execute(
                select(Payment)
                .where(Payment.order_id == int(order.id))
                .order_by(Payment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if payment is None:
        has_fail = await _has_open_delivery_failure(
            session, order_id=int(order.id), payment_id=None, known=open_failures
        )
        incomplete = not _order_work_complete(order)
        return _base(
            kind="approved_fulfill" if incomplete else "unapprovable",
            payment=None,
            order=order,
            has_open_delivery_failure=has_fail,
            can_resume_fulfill=incomplete,
            can_resend_telegram=_order_work_complete(order),
            label_fa="بدون پرداخت" if not incomplete else "گیرکرده بدون پرداخت",
            alert_fa="سفارش بدون ردیف پرداخت مرتبط",
            detail_fa="برای این سفارش پرداختی پیدا نشد.",
            log_code="payrev.order_no_payment",
        )
    return await diagnose_payment_review(session, payment, order=order, open_failures=open_failures)


async def diagnose_order_deliveries(
    session: AsyncSession,
    orders: Iterable[Order],
    payments_by_order: dict[int, Payment],
) -> dict[int, PaymentReviewDiagnosis]:
    """Diagnose a page of delivery rows; ``payments_by_order`` holds the newest payment of each.

    Open delivery failures of all of them are read in one query, so the page costs the same for
    one order or a hundred. One row that cannot be diagnosed is left out, not the whole page.
    """
    orders = list(orders)
    open_failures = await load_open_failure_keys(
        session,
        order_ids=[int(o.id) for o in orders],
        payment_ids=[int(p.id) for p in payments_by_order.values()],
    )
    diagnoses: dict[int, PaymentReviewDiagnosis] = {}
    for order in orders:
        try:
            diagnoses[int(order.id)] = await diagnose_order_delivery(
                session,
                order,
                payment=payments_by_order.get(int(order.id)),
                payment_loaded=True,
                open_failures=open_failures,
            )
        except Exception:  # display only: the row is shown without a diagnosis
            logger.debug("delivery diagnosis failed order=%s", order.id, exc_info=True)
    return diagnoses


def log_payment_review_diagnosis(
    diag: PaymentReviewDiagnosis,
    *,
    actor_tg: int | None = None,
    source: str = "payrev",
) -> None:
    logger.info(
        "%s diagnosis kind=%s code=%s payment_id=%s order_id=%s "
        "pay_status=%s order_status=%s service=%s fail=%s actor=%s",
        source,
        diag.kind,
        diag.log_code,
        diag.payment_id,
        diag.order_id,
        diag.payment_status,
        diag.order_status,
        diag.has_service,
        diag.has_open_delivery_failure,
        actor_tg,
        extra=diag.as_log_extra(),
    )
