"""The customer notice of an unpaid order that expired (``pending_order_expiry``); kept apart so the notice
sender can import it without the order services."""

from __future__ import annotations

EXPIRED_ACTION = "order_expired"
EXPIRED_TEXT = (
    "⌛ <b>خرید نیمه‌تمام لغو شد</b>\n"
    "سفارش <code>{name}</code> در مهلت تعیین‌شده پرداخت نشد و خودکار لغو شد. "
    "اگر هنوز آن را می‌خواهید، دوباره خرید کنید."
)
