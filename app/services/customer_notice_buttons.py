"""Buttons under a customer notice: what the notice is about, the way out it offers, and support.

Each one opens its screen in a panel under the notice (``notice_links``), so the notice stays readable
while the customer renews, buys a pack or writes to support.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.button_styles import style_kwargs
from app.services.notice_links import notice_open_data
from app.services.support_button import SUPPORT_BUTTON_LABEL, SUPPORT_NOTICE_CALLBACK, support_row

WALLET = ("👛 کیف پول", "nv:w:home")
CLUB = ("⭐ باشگاه مشتریان", "nv:loy:home")
MY_SERVICES = ("📦 سرویس‌های من", "svc:list")
BUY_NEW = ("🛒 خرید سرویس جدید", "shop:list")
RENEW_LABEL = "🔄 تمدید سرویس"
VOLUME_PACK_LABEL = "📦 خرید بستهٔ حجم"
AUTO_RENEW_LABEL = "⚙️ روشن کردن تمدید خودکار"
# Style id of the renew button in «رنگ دکمه‌ها»; the id stays so saved colours keep working.
RENEW_STYLE_ID = "one_tap_renew"


def _open(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=notice_open_data(data))


def renew_button(service_id: int, ui: dict | None = None) -> InlineKeyboardButton:
    """Opens the renewal choices of one service under the notice."""
    return InlineKeyboardButton(
        text=RENEW_LABEL,
        callback_data=notice_open_data(f"svc:renew:{int(service_id)}"),
        **style_kwargs(ui, RENEW_STYLE_ID, fallback="primary"),
    )


def auto_renew_button(service_id: int) -> InlineKeyboardButton:
    """Opens the automatic-renewal settings of one service under the notice."""
    return _open(AUTO_RENEW_LABEL, f"svc:auto:{int(service_id)}")


def volume_pack_button(service_id: int) -> InlineKeyboardButton:
    return _open(VOLUME_PACK_LABEL, f"svc:addon:{int(service_id)}:volume")


def services_button() -> InlineKeyboardButton:
    return _open(*MY_SERVICES)


def buy_new_button() -> InlineKeyboardButton:
    return _open(*BUY_NEW)


def support_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=SUPPORT_BUTTON_LABEL, callback_data=SUPPORT_NOTICE_CALLBACK)


def help_row(*, services: bool = True) -> list[InlineKeyboardButton]:
    """The last row of a service notice: the customer's services (when it still lists them) and support."""
    return [services_button(), support_button()] if services else [support_button()]


def customer_notice_markup(
    *, wallet: bool = False, club: bool = False, services: bool = False, support: bool = False
) -> InlineKeyboardMarkup | None:
    row = [_open(text, data) for wanted, (text, data) in ((wallet, WALLET), (club, CLUB), (services, MY_SERVICES)) if wanted]
    rows = [row] if row else []
    if support:
        rows.append(support_row())
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
