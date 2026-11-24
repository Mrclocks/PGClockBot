"""Text and buttons of the customer's «لغو سرویس» screens in the bot.

The screen shows the open request, or what a new request would return under the shop's settings.
Rendering only: the handlers in ``services`` load the offer and run the request.
"""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import keyboards as kb
from app.services.cancellation_refund import RefundQuote
from app.services.formatting import format_toman
from app.services.numbers import to_fa_digits
from app.services.service_cancellation_settings import REFUND_NONE, REFUND_REMAINING, CancellationPolicy
from app.services.service_cancellations import CancellationOffer

TITLE = "📝 لغو سرویس"
MANUAL_NOTE = "پس از ثبت، ادمین درخواست را بررسی می‌کند و تا آن موقع سرویس فعال می‌ماند."
AUTO_NOTE = "با تأیید شما سرویس همان لحظه غیرفعال و مبلغ به کیف پول همین فروشگاه واریز می‌شود. این کار برگشت‌پذیر نیست."
NO_QUOTE_NOTE = "پنل الان پاسخ نداد؛ مبلغ برگشتی را ادمین تعیین می‌کند."
REASON_PROMPT = "دلیل لغو را بنویسید (حداکثر ۱۰۰۰ نویسه)."


def new_request_callback(service_id: int) -> str:
    return f"svc:cancelnew:{int(service_id)}"


def view_callback(service_id: int) -> str:
    return f"svc:cancel:{int(service_id)}"


def confirm_callback(service_id: int) -> str:
    return f"svc:cancelok:{int(service_id)}"


def quote_lines(quote: RefundQuote | None, policy: CancellationPolicy, currency: str) -> list[str]:
    """What a new request returns, in the shop's words; the panel's figures are approximate."""
    if policy.refund == REFUND_NONE:
        return ["این فروشگاه برای لغو سرویس مبلغی برنمی‌گرداند."]
    if quote is None:
        return [NO_QUOTE_NOTE]
    lines = [f"💰 مبلغ برگشتی تقریبی: <b>{format_toman(quote.amount, currency)}</b>"]
    if policy.refund == REFUND_REMAINING:
        lines.append(
            f"بر اساس {to_fa_digits(quote.remaining_percent)}٪ باقی‌مانده از "
            f"{format_toman(quote.paid_amount, currency)} پرداختی دورهٔ فعلی"
        )
    if policy.fee_percent:
        lines.append(f"کارمزد لغو ({to_fa_digits(policy.fee_percent)}٪) از این مبلغ کم شده است.")
    if not policy.is_auto:
        lines.append("مبلغ نهایی را ادمین تعیین می‌کند.")
    return lines


def _status_lines(status: dict, currency: str) -> list[str]:
    lines = [f"درخواست #{int(status['id'])}: {html.escape(status['label'])}"]
    if status["operator_note"]:
        lines.append(html.escape(status["operator_note"]))
    if status["refund_amount"] is not None:
        lines.append("اعتبار برگشتی: " + format_toman(int(status["refund_amount"]), currency))
    return lines


def offer_screen(offer: CancellationOffer, service_id: int, currency: str) -> tuple[str, InlineKeyboardMarkup]:
    parts = [TITLE]
    rows: list[list[InlineKeyboardButton]] = []
    if offer.status:
        parts.append("\n".join(_status_lines(offer.status, currency)))
    if offer.can_request:
        parts.append("\n".join(quote_lines(offer.quote, offer.policy, currency)))
        parts.append(AUTO_NOTE if offer.policy.is_auto else MANUAL_NOTE)
        rows.append([InlineKeyboardButton(text="ثبت درخواست و دلیل لغو", callback_data=new_request_callback(service_id))])
    elif offer.status:
        rows.append([InlineKeyboardButton(text="به‌روزرسانی وضعیت", callback_data=view_callback(service_id))])
    else:
        parts.append(html.escape(offer.blocked_reason or ""))
    rows.append([kb.back_button(None, f"svc:more:{int(service_id)}")])
    return "\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_screen(offer: CancellationOffer, service_id: int, currency: str) -> tuple[str, InlineKeyboardMarkup]:
    """Last step before an automatic cancellation, which cannot be undone."""
    text = "\n\n".join([
        "⚠️ <b>تأیید لغو سرویس</b>",
        "\n".join(quote_lines(offer.quote, offer.policy, currency)),
        AUTO_NOTE,
    ])
    rows = [
        [InlineKeyboardButton(text="✅ تأیید و لغو سرویس", callback_data=confirm_callback(service_id))],
        [kb.back_button(None, view_callback(service_id))],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def result_text(status: dict, currency: str) -> str:
    if status["status"] == "approved":
        text = "✅ سرویس لغو شد."
        if status["refund_amount"]:
            text += f"\n💰 {format_toman(int(status['refund_amount']), currency)} به کیف پول شما برگشت."
        return text
    return f"درخواست #{int(status['id'])} ثبت شد: {html.escape(status['label'])}"
