"""Buttons under staff reports and alerts that open the screen to follow up from.

Each button opens its screen in a panel under the notice (``notice_links``), so the report or alert
stays readable. The screens re-check the person's access, as on any tap.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.notice_links import notice_markup

PLATFORM_DASHBOARD = ("📊 داشبورد", "adm:dash")
SHOP_PANEL = ("🤝 پنل نماینده", "nv:res:home")
RESELLERS_HUB = ("🤝 نمایندگان", "nv:adm:resellers")
PASARGUARD_HUB = ("🖥 پاسارگارد", "nv:adm:pg")
RESELLER_CARD_LABEL = "🤝 نماینده"
# The «panel is down» scenario's confirmation, and the command center where it is ended.
OUTAGE_SCENARIO = ("🚨 سناریوی «پنل قطع است»", "cmd:sc:down")
COMMAND_CENTER = ("⚡ مرکز فرمان", "cmd:h")


def _one(label_and_data: tuple[str, str]) -> InlineKeyboardMarkup:
    text, data = label_and_data
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)]])
    return notice_markup(markup) or markup


def platform_report_markup() -> InlineKeyboardMarkup:
    return _one(PLATFORM_DASHBOARD)


def shop_panel_markup() -> InlineKeyboardMarkup:
    return _one(SHOP_PANEL)


def subordinate_report_markup(*, shop: bool) -> InlineKeyboardMarkup:
    """The digest of a parent's direct shops: the platform resellers hub, or the shop's own panel."""
    return _one(SHOP_PANEL if shop else RESELLERS_HUB)


def pasarguard_markup() -> InlineKeyboardMarkup:
    return _one(PASARGUARD_HUB)


def link_alert_markup(*, is_down: bool) -> InlineKeyboardMarkup:
    """Under a panel link alert: the PasarGuard hub, and starting (down) or ending (up) the outage scenario."""
    follow_up = OUTAGE_SCENARIO if is_down else COMMAND_CENTER
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in (PASARGUARD_HUB, follow_up)]
    )
    return notice_markup(markup) or markup


def reseller_card_markup(user_id: int) -> InlineKeyboardMarkup:
    return _one((RESELLER_CARD_LABEL, f"adm:resellers:view:{int(user_id)}"))
