"""Ready-made reasons a reviewer taps when rejecting a receipt or a cancellation request.

Each shop edits its own lists in settings, one reason per line. The chosen text is stored on
the record (``Payment.review_note`` / the cancellation's operator note) and shown to the
customer, so a reviewer explains a rejection with one tap instead of typing.
"""

from __future__ import annotations

import html
from typing import Any

PAYMENT_REASONS_KEY = "payment_reject_reasons"
CANCELLATION_REASONS_KEY = "service_cancel_reject_reasons"
# Bot callbacks carry one digit (1-9) for the chosen reason.
MAX_REASONS = 8
MAX_REASON_LENGTH = 120
BUTTON_LABEL_LENGTH = 40
# Stored on a payment rejected without a reason, as before reasons existed.
NO_REASON_NOTE = "rejected"

DEFAULT_PAYMENT_REASONS = "\n".join((
    "مبلغ رسید با مبلغ سفارش یکی نیست.",
    "رسید ناخوانا است؛ لطفاً تصویر واضح‌تری بفرستید.",
    "واریزی با این مشخصات به حساب ما نرسیده است.",
    "این رسید قبلاً برای پرداخت دیگری ثبت شده است.",
))
DEFAULT_CANCELLATION_REASONS = "\n".join((
    "درخواست با شرایط لغو فروشگاه مطابقت ندارد.",
    "سرویس هنوز قابل استفاده است؛ در صورت نیاز با پشتیبانی تماس بگیرید.",
    "این درخواست تکراری است.",
))
REASON_DEFAULTS = {
    PAYMENT_REASONS_KEY: DEFAULT_PAYMENT_REASONS,
    CANCELLATION_REASONS_KEY: DEFAULT_CANCELLATION_REASONS,
}


def parse_reasons(raw: Any) -> list[str]:
    """The usable reasons of a setting: trimmed, non-empty, distinct, bounded in count and length."""
    if not isinstance(raw, str):
        return []
    reasons: list[str] = []
    for line in raw.splitlines():
        reason = " ".join(line.split())[:MAX_REASON_LENGTH]
        if reason and reason not in reasons:
            reasons.append(reason)
        if len(reasons) == MAX_REASONS:
            break
    return reasons


def cancellation_reasons(ui: dict[str, Any]) -> list[str]:
    """A cancellation is always rejected with a reason, so an emptied list falls back to the defaults."""
    return parse_reasons(ui.get(CANCELLATION_REASONS_KEY)) or parse_reasons(DEFAULT_CANCELLATION_REASONS)


def reason_at(reasons: list[str], number: int) -> str:
    """The reason a button numbered from 1 stands for; the list may have changed since it was shown."""
    if type(number) is not int or not 1 <= number <= len(reasons):
        raise ValueError("این دلیل دیگر در فهرست نیست؛ دوباره «رد» را بزنید")
    return reasons[number - 1]


def button_label(reason: str) -> str:
    return reason if len(reason) <= BUTTON_LABEL_LENGTH else reason[: BUTTON_LABEL_LENGTH - 1] + "…"


def with_reason(text: str, send_kw: dict[str, Any], reason: str | None) -> str:
    """Add the reason under a built notice, escaped for HTML unless the notice is sent as entities."""
    if not reason:
        return text
    plain = send_kw.get("parse_mode", "HTML") is None
    line = f"دلیل: {reason}" if plain else f"<b>دلیل:</b> {html.escape(reason)}"
    return f"{text}\n\n{line}"
