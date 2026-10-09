"""Inline hub keyboards and chrome-free present helpers (nav_mode=inline).

Contextual screens send/edit a single message with an InlineKeyboard.
The main ReplyKeyboard is left unchanged (set on /start or legacy heal).
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards import _ikb, _style, _t

logger = logging.getLogger(__name__)

# Filler texts that must never be sent as standalone messages in inline mode.
FILLER_CHROME_TEXTS = frozenset(
    {
        "⌨️",
        "·",
        "📱",
        "⌨️ منوی اصلی",
        "⌨️ منوی فروشگاه",
        "⌨️ پشتیبانی",
        "⌨️ باشگاه مشتریان",
        "⌨️ درخواست نمایندگی",
        "🏠 منوی اصلی",
    }
)


def is_filler_chrome_text(text: str | None) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t in FILLER_CHROME_TEXTS:
        return True
    if t.startswith("⌨️"):
        return True
    return False


async def present_inline_only(
    message: Message,
    *,
    text: str,
    inline: InlineKeyboardMarkup | None,
    **send_kw: Any,
) -> Message | None:
    """Send one content message with inline markup — no reply-chrome follow-up."""
    try:
        return await message.answer(text, reply_markup=inline, **send_kw)
    except Exception:
        logger.warning("present_inline_only failed", exc_info=True)
        return None


async def safe_edit_inline(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    **send_kw: Any,
) -> bool:
    """Edit in place; on failure answer a short notice (no spam send of full hub)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, **send_kw)
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return True
        logger.info("safe_edit_inline: %s", e)
        return False
    except Exception:
        logger.warning("safe_edit_inline failed", exc_info=True)
        return False


def wallet_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            _ikb(
                _t(ui, "btn_wallet_topup") or "➕ شارژ",
                callback_data="nv:w:topup",
                ui=ui,
                label_key="btn_wallet_topup",
                style=_style(ui, "wallet_topup", fallback="primary"),
            ),
            _ikb(
                _t(ui, "btn_wallet_tx") or "📜 تراکنش‌ها",
                callback_data="nv:w:tx",
                ui=ui,
                label_key="btn_wallet_tx",
                style=_style(ui, "wallet_tx"),
            ),
        ],
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="menu:home",
                ui=ui,
                label_key="btn_back",
                style=_style(ui, "back"),
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_hub_keyboard(
    ui: dict | None = None,
    *,
    contact_rows: list[list[InlineKeyboardButton]] | None = None,
    include_reseller_apply: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            _ikb(
                _t(ui, "btn_support_new") or "✉️ تیکت جدید",
                callback_data="nv:s:new",
                ui=ui,
                label_key="btn_support_new",
                style=_style(ui, "support_new", fallback="primary"),
            ),
            _ikb(
                _t(ui, "btn_support_list") or "📋 تیکت‌های من",
                callback_data="nv:s:list",
                ui=ui,
                label_key="btn_support_list",
                style=_style(ui, "support_list"),
            ),
        ]
    ]
    if contact_rows:
        rows.extend(contact_rows)
    if include_reseller_apply:
        rows.append(
            [
                _ikb(
                    _t(ui, "btn_reseller_apply") or "🤝 درخواست نمایندگی",
                    callback_data="nv:resapply",
                    ui=ui,
                    label_key="btn_reseller_apply",
                    style=_style(ui, "reseller_apply"),
                )
            ]
        )
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="menu:home",
                ui=ui,
                label_key="btn_back",
                style=_style(ui, "back"),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def loyalty_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Inline club hub from loyalty_submenu_order (same labels as reply submenu)."""
    from app.bot.reply_keyboards import (
        REPLY_ACTION_LOY_HISTORY,
        REPLY_ACTION_LOY_POINTS,
        REPLY_ACTION_LOY_REFERRAL,
        REPLY_ACTION_LOY_REWARDS,
        REPLY_ACTION_LOY_WHEEL,
        _loyalty_submenu_entries,
    )

    cb_map = {
        REPLY_ACTION_LOY_REFERRAL: "nv:loy:ref",
        REPLY_ACTION_LOY_POINTS: "nv:loy:pts",
        REPLY_ACTION_LOY_REWARDS: "nv:loy:rew",
        REPLY_ACTION_LOY_WHEEL: "nv:loy:wheel",
        REPLY_ACTION_LOY_HISTORY: "nv:loy:hist",
    }
    style_map = {
        REPLY_ACTION_LOY_REFERRAL: "loy_referral",
        REPLY_ACTION_LOY_POINTS: "loy_points",
        REPLY_ACTION_LOY_REWARDS: "loy_rewards",
        REPLY_ACTION_LOY_WHEEL: "loy_wheel",
        REPLY_ACTION_LOY_HISTORY: "loy_history",
    }
    entries = _loyalty_submenu_entries(ui)
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, style_map[key], fallback="primary"),
        )
        for key, text in entries
        if key in cb_map
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="menu:home",
                ui=ui,
                label_key="btn_back",
                style=_style(ui, "back"),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def welcome_cta_keyboard(
    ui: dict | None = None,
    *,
    show_trial: bool = False,
    show_reseller_apply: bool = False,
) -> InlineKeyboardMarkup | None:
    """Optional CTAs for welcome (used when welcome has no ReplyKeyboard dual need)."""
    rows: list[list[InlineKeyboardButton]] = []
    if show_trial:
        rows.append(
            [
                _ikb(
                    "🎁 دریافت تست رایگان",
                    callback_data="nv:trial",
                    ui=ui,
                    style=_style(ui, "shop_kind_trial", fallback="primary"),
                )
            ]
        )
    if show_reseller_apply:
        rows.append(
            [
                _ikb(
                    _t(ui, "btn_reseller_apply") or "🤝 درخواست نمایندگی",
                    callback_data="nv:resapply",
                    ui=ui,
                    label_key="btn_reseller_apply",
                    style=_style(ui, "reseller_apply"),
                )
            ]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def miniapp_reply_button(ui: dict | None = None):
    """KeyboardButton with web_app for the stable main ReplyKeyboard (platform bot)."""
    from aiogram.types import KeyboardButton, WebAppInfo

    from app.config import get_settings
    from app.services.users import current_shop_reseller_id

    if current_shop_reseller_id():
        return None
    settings = get_settings()
    if not settings.miniapp_enabled:
        return None
    url = (settings.miniapp_url or "").strip()
    if not url.startswith("https://"):
        return None
    from app.bot.keyboards import _menu_order

    if "miniapp" not in _menu_order(ui):
        return None
    text = (_t(ui, "btn_miniapp") or "📱 مینی‌اپ").strip() or "📱 مینی‌اپ"
    return KeyboardButton(text=text[:64], web_app=WebAppInfo(url=url))
