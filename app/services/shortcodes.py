"""Context-aware shortcode registry for customizable Telegram messages.

Shortcodes are an allowlist of simple ``{name}`` placeholders only.
No expressions, attribute traversal, imports, or evaluation.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.services.safe_format import safe_format

# key → short Persian explanation (shown in settings help UI)
SHORTCODE_HELP: dict[str, str] = {
    "name": "نام کاربر",
    "username": "نام کاربری سرویس",
    "status": "وضعیت فعلی سرویس",
    "service": "شناسه یا نام سرویس",
    "plan": "نام پلن",
    "traffic": "حجم کل سرویس",
    "traffic_used": "حجم مصرف‌شده",
    "traffic_remaining": "حجم باقی‌مانده",
    "expire": "تاریخ انقضا",
    "days_remaining": "روزهای باقی‌مانده",
    "price": "مبلغ سفارش",
    "amount": "مبلغ (پرداخت/شارژ)",
    "currency": "واحد پول",
    "order_id": "شماره سفارش",
    "payment_id": "شماره پرداخت",
    "card": "شماره کارت",
    "holder": "نام صاحب کارت",
    "code": "کد دعوت",
    "link": "لینک دعوت",
    "referral_code": "کد دعوت",
    "referral_count": "تعداد دعوت‌شدگان",
    "points": "امتیاز وفاداری",
    "wallet_balance": "موجودی کیف پول",
    "url": "لینک اشتراک",
    "asset": "نوع رمزارز",
    "network": "شبکه رمزارز",
    "address": "آدرس ولت",
    "gateway_name": "نام درگاه پرداخت",
}

# Deterministic sample data for live preview (never real customer data)
SAMPLE_VALUES: dict[str, str] = {
    "name": "نمونه کاربر",
    "username": "user_demo",
    "status": "فعال",
    "service": "سرویس نمونه",
    "plan": "پلن ۳۰ روزه",
    "traffic": "100 GB",
    "traffic_used": "27 GB",
    "traffic_remaining": "73 GB",
    "expire": "۱۴۰۵/۰۱/۱۵",
    "days_remaining": "۲۲",
    "price": "۵۰٬۰۰۰",
    "amount": "۵۰٬۰۰۰ تومان",
    "currency": "تومان",
    "order_id": "1024",
    "payment_id": "2048",
    "card": "6037-****-****-1234",
    "holder": "نام دارنده",
    "code": "CLK42",
    "link": "https://t.me/example_bot?start=CLK42",
    "referral_code": "CLK42",
    "referral_count": "۳",
    "points": "۱۲۰",
    "wallet_balance": "۲۵٬۰۰۰ تومان",
    "url": "https://example.com/sub/demo",
    "asset": "USDT",
    "network": "TRC20",
    "address": "TXampleAddressForPreviewOnly",
    "gateway_name": "زرین‌پال",
}

# Setting key → shortcodes valid in that message context
MESSAGE_SHORTCODES: dict[str, tuple[str, ...]] = {
    "welcome_text": ("name",),
    "guide_text": (),
    "faq_text": (),
    "support_text": (),
    "referral_text": ("code", "link", "referral_code", "referral_count", "points"),
    "empty_services_text": (),
    "shop_empty_text": (),
    "delivery_title": (),
    "purchase_success_text": (
        "order_id",
        "amount",
        "price",
        "currency",
        "plan",
        "service",
        "username",
        "status",
        "traffic",
        "traffic_used",
        "traffic_remaining",
        "expire",
        "days_remaining",
    ),
    "wallet_success_text": ("amount", "payment_id", "wallet_balance", "currency"),
    "payment_reject_text": ("order_id", "amount", "name"),
    "qr_caption": ("url", "username", "traffic", "expire", "plan"),
    "card_pay_text": ("amount", "card", "holder", "order_id", "payment_id"),
    "gateway_pay_text": ("amount", "order_id", "payment_id", "gateway_name", "name"),
    "gateway_link": ("amount", "order_id", "payment_id"),
    "crypto_pay_text": ("amount", "asset", "network", "address", "order_id", "payment_id"),
}


def shortcodes_for(setting_key: str) -> list[tuple[str, str]]:
    """Return ``(shortcode, persian_help)`` pairs for a settings field."""
    keys = MESSAGE_SHORTCODES.get(setting_key) or ()
    return [(k, SHORTCODE_HELP.get(k, k)) for k in keys]


def has_shortcodes(setting_key: str) -> bool:
    return bool(MESSAGE_SHORTCODES.get(setting_key))


def render_user_message(
    template: str | None,
    default: str = "",
    *,
    mapping: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    """Render a customizable user-facing template safely.

    - Allowlisted ``{name}`` only (via ``safe_format``)
    - Dynamic values HTML-escaped for Telegram parse_mode=HTML
    - Empty / failed render falls back to *default*
    """
    raw = (template or "").strip()
    if not raw:
        return default
    try:
        out = safe_format(raw, mapping, escape_html=True, **kwargs).strip()
    except Exception:
        return default
    return out if out else default


def preview_fill(text: str | None) -> str:
    """Replace known shortcodes with sample preview values."""
    return safe_format(text or "", SAMPLE_VALUES, escape_html=False)
