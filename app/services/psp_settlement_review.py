"""Gateway payments that were paid but cannot be applied go to the shop's staff.

A PSP gateway keeps the customer's money once the payment succeeds; verify only reads
it. When the paid amount differs, or the payment/order can no longer be approved (the
customer picked another method, the order was paid another way or cancelled), the
settlement is closed for review and the staff are told, instead of being re-verified
forever. The saved verify details let staff find the transaction for a refund.
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Payment, PaymentSettlement, PaymentStatus, SettlementStatus
from app.services.payment_providers.base import VerifyResult

logger = logging.getLogger(__name__)

REVIEW_PREFIX = "review:"
REVIEW_IN_PROGRESS_MESSAGE = "این پرداخت در حال بررسی است؛ با پشتیبانی در تماس باشید"
REVIEW_NOTICE_TITLE = "⚠️ پرداخت درگاه آنلاین نیاز به بررسی دارد"

REVIEW_REASONS = {
    "payment_not_pending": (
        "مبلغ در درگاه پرداخت شده ولی این پرداخت در ربات دیگر باز نیست "
        "(روش پرداخت عوض شده یا سفارش لغو شده). خودکار اعمال نشد؛ "
        "تحویل دستی یا استرداد وجه لازم است."
    ),
    "order_closed": (
        "مبلغ در درگاه پرداخت شده ولی سفارش قبلاً پرداخت، تحویل یا لغو شده است. "
        "خودکار اعمال نشد؛ احتمالاً پرداخت تکراری است و استرداد وجه لازم است."
    ),
    "amount_mismatch": "مبلغ پرداخت‌شده در درگاه با مبلغ این پرداخت یکی نیست. خودکار اعمال نشد.",
    "invoice_mismatch": "شناسهٔ فاکتورِ تراکنش پرداخت‌شده با این پرداخت یکی نیست. خودکار اعمال نشد.",
}

# Orders approve_payment can still move to PAID.
_APPROVABLE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.AWAITING_RECEIPT.value,
        OrderStatus.AWAITING_APPROVAL.value,
    }
)
_REVIEWABLE_STATUSES = (
    SettlementStatus.CREATED.value,
    SettlementStatus.AWAITING.value,
    SettlementStatus.SETTLING.value,
)


def is_in_review(settlement: PaymentSettlement) -> bool:
    return settlement.status == SettlementStatus.FAILED.value and str(
        settlement.error_message or ""
    ).startswith(REVIEW_PREFIX)


async def unusable_payment_reason(session: AsyncSession, payment: Payment) -> str | None:
    """Why a verified gateway payment cannot be approved, or None when it can."""
    if payment.status == PaymentStatus.APPROVED.value:
        return None
    if payment.status != PaymentStatus.PENDING.value:
        return "payment_not_pending"
    if payment.is_wallet_topup or not payment.order_id:
        return None
    order = await session.get(Order, int(payment.order_id))
    if order is not None and order.status not in _APPROVABLE_ORDER_STATUSES:
        return "order_closed"
    return None


def merge_verify_details(provider_payload: str | None, verified: VerifyResult) -> str:
    """Add what the gateway reported (never the raw response) to the saved payload."""
    try:
        data: Any = json.loads(provider_payload or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["verify"] = {
        "reference": verified.reference,
        "paid_at": verified.paid_at,
        "amount": verified.amount,
    }
    return json.dumps(data, ensure_ascii=False)[:4000]


async def send_to_review(
    session: AsyncSession,
    settlement: PaymentSettlement,
    reason: str,
    *,
    verified: VerifyResult,
) -> None:
    """Close the settlement for staff review once; only the caller that closes it notifies."""
    claimed = await session.execute(
        update(PaymentSettlement)
        .where(
            PaymentSettlement.id == int(settlement.id),
            PaymentSettlement.status.in_(_REVIEWABLE_STATUSES),
        )
        .values(
            status=SettlementStatus.FAILED.value,
            error_message=f"{REVIEW_PREFIX}{reason}",
            provider_payload=merge_verify_details(settlement.provider_payload, verified),
            checkout_token=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(settlement)
    if claimed.rowcount == 1:
        logger.warning(
            "psp settlement sent to review settlement=%s reason=%s", settlement.id, reason
        )
        await _notify_staff(session, settlement, reason, verified=verified)


async def _notify_staff(
    session: AsyncSession, settlement: PaymentSettlement, reason: str, *, verified: VerifyResult
) -> None:
    # Local import: notifications pulls in bot keyboards, which import payment modules.
    from app.services.notifications import notify_settlement_review

    payment = await session.get(Payment, int(settlement.payment_id))
    if payment is None:
        return
    detail = REVIEW_REASONS.get(reason, reason)
    if verified.reference:
        detail = f"{detail}\nکد رهگیری درگاه: {html.escape(verified.reference)}"
    if verified.amount is not None and int(verified.amount) != int(payment.amount):
        detail = f"{detail}\nمبلغ پرداخت‌شده: {int(verified.amount)}"
    try:
        await notify_settlement_review(
            session,
            payment,
            shop_id=settlement.shop_owner_id,
            reason=detail,
            title=REVIEW_NOTICE_TITLE,
        )
    except Exception:
        # The review marker is saved; staff still see it on the settlement.
        logger.warning("psp review notice failed settlement=%s", settlement.id, exc_info=True)
