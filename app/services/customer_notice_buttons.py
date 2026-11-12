"""Buttons under a customer notice: the wallet, the club or the services it is about, and support.

Each one opens its screen in a panel under the notice (``notice_links``), so the notice stays.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.notice_links import notice_open_data
from app.services.support_button import support_row

WALLET = ("👛 کیف پول", "nv:w:home")
CLUB = ("⭐ باشگاه مشتریان", "nv:loy:home")
MY_SERVICES = ("📦 سرویس‌های من", "svc:list")


def customer_notice_markup(
    *, wallet: bool = False, club: bool = False, services: bool = False, support: bool = False
) -> InlineKeyboardMarkup | None:
    row = [
        InlineKeyboardButton(text=text, callback_data=notice_open_data(data))
        for wanted, (text, data) in ((wallet, WALLET), (club, CLUB), (services, MY_SERVICES))
        if wanted
    ]
    rows = [row] if row else []
    if support:
        rows.append(support_row())
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
