"""A risk label for a card-to-card receipt, from the customer's history in the same shop.

Reviewers used to see every receipt the same way; only a reused image was flagged. Each new
receipt now gets a level and the reasons behind it, from one aggregate query over the
customer's payments in the payment's own shop:

- the same image was sent for another payment (``receipt_fingerprints``);
- a payment of this customer was rejected in the last ``REJECTION_WINDOW``;
- several receipts arrived within ``BURST_WINDOW``;
- the customer has no approved payment in this shop yet (shown, but not enough alone).

The label is advice for a person. Only when the shop turns on ``HOLD_KEY`` does a «بررسی دقیق»
receipt skip automatic approval and wait for a reviewer instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Payment, PaymentStatus

ENABLED_KEY = "receipt_risk_enabled"
HOLD_KEY = "receipt_risk_hold_auto_approve"
RISK_DEFAULTS = {ENABLED_KEY: "1", HOLD_KEY: "0"}
LEVEL_LOW = "low"
LEVEL_REVIEW = "review"
BURST_WINDOW = timedelta(minutes=15)
BURST_RECEIPTS = 3
REJECTION_WINDOW = timedelta(days=30)
REASON_DUPLICATE = "همین تصویر برای پرداخت دیگری هم ارسال شده"
REASON_REJECTED = "پرداخت ردشده در ۳۰ روز گذشته"
REASON_BURST = "چند رسید در کمتر از ۱۵ دقیقه"
REASON_NEW = "مشتری تازه: هنوز پرداخت تأییدشده‌ای در این فروشگاه ندارد"
STRONG_REASONS = frozenset({REASON_DUPLICATE, REASON_REJECTED, REASON_BURST})

WEB_FIELDS: list[tuple[str, str, str, str]] = [
    (
        ENABLED_KEY,
        "برچسب ریسک رسید",
        "toggle",
        "کنار هر رسید کارت‌به‌کارت برای بررسی‌کننده نوشته می‌شود «کم‌ریسک» یا «بررسی دقیق» و دلیلش: "
        "رسید تکراری، پرداخت ردشدهٔ اخیر، چند رسید پشت‌سرهم، یا مشتری تازه.",
    ),
    (
        HOLD_KEY,
        "رسید «بررسی دقیق» خودکار تأیید نشود",
        "toggle",
        "اگر تأیید خودکار رسید روشن است، رسیدی که برچسب «بررسی دقیق» دارد منتظر تأیید دستی می‌ماند.",
    ),
]
BOT_FIELDS: list[tuple[str, str, str]] = [(key, label, kind) for key, label, kind, _help in WEB_FIELDS]


@dataclass(frozen=True, slots=True)
class ReceiptRisk:
    level: str
    reasons: tuple[str, ...]

    @property
    def needs_review(self) -> bool:
        return self.level == LEVEL_REVIEW


def classify(reasons: list[str]) -> ReceiptRisk:
    """«بررسی دقیق» on any strong reason, or on two reasons together; otherwise «کم‌ریسک»."""
    strong = any(reason in STRONG_REASONS for reason in reasons)
    level = LEVEL_REVIEW if strong or len(reasons) >= 2 else LEVEL_LOW
    return ReceiptRisk(level=level, reasons=tuple(reasons))


def _flag(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _shop_scope(shop_id: int | None):
    # A top-up carries its purse; an order payment belongs to the order's shop.
    shop = func.coalesce(Payment.wallet_shop_id, Order.reseller_id)
    return shop.is_(None) if shop_id is None else shop == int(shop_id)


async def assess_receipt(
    session: AsyncSession, payment: Payment, *, shop_id: int | None, duplicates: int, now: datetime | None = None
) -> ReceiptRisk:
    """The risk of ``payment``'s receipt; one query, scoped to ``shop_id`` (None = platform)."""
    now = now or datetime.now(timezone.utc)
    prior_approved, rejections, recent = (await session.execute(
        select(
            _flag(and_(Payment.status == PaymentStatus.APPROVED.value, Payment.id < int(payment.id))),
            _flag(and_(Payment.status == PaymentStatus.REJECTED.value, Payment.created_at >= now - REJECTION_WINDOW)),
            _flag(Payment.receipt_at >= now - BURST_WINDOW),
        )
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(Payment.user_id == int(payment.user_id), _shop_scope(shop_id))
    )).one()
    reasons = [
        reason for reason, present in (
            (REASON_DUPLICATE, duplicates > 0),
            (REASON_REJECTED, int(rejections) > 0),
            (REASON_BURST, int(recent) >= BURST_RECEIPTS),
            (REASON_NEW, int(prior_approved) == 0),
        ) if present
    ]
    return classify(reasons)


def risk_note(risk: ReceiptRisk) -> str:
    """The reviewer's line; reasons are fixed texts, never customer input."""
    if not risk.needs_review:
        extra = f" ({risk.reasons[0]})" if risk.reasons else ""
        return f"🛡 ریسک رسید: کم‌ریسک{extra}"
    return "⚠️ ریسک رسید: بررسی دقیق\n" + "\n".join(f"• {reason}" for reason in risk.reasons)
