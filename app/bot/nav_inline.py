"""Inline hub keyboards and chrome-free present helpers (nav_mode=inline).

Pure-inline staff contract (see ``app.bot.nav_chrome``):
- ReplyKeyboard = level-0 only (user menu + one staff entry).
- Nested admin/reseller menus live on one edited inline panel.
- Never restore a reply submenu after cancel under inline mode.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards import _ikb, _style, _t

logger = logging.getLogger(__name__)

# FSM keys for the single live nav panel (edit instead of spam).
NAV_PANEL_CHAT_KEY = "_nav_panel_chat_id"
NAV_PANEL_MSG_KEY = "_nav_panel_msg_id"

# Filler texts that must never be sent as standalone messages in inline mode.
FILLER_CHROME_TEXTS = frozenset(
    {
        "⌨️",
        "·",
        "📱",
        "⬇️",
        "⌨️ منوی اصلی",
        "⌨️ منوی فروشگاه",
        "⌨️ پشتیبانی",
        "⌨️ باشگاه مشتریان",
        "⌨️ درخواست نمایندگی",
        "⌨️ بکاپ",
        "⌨️ نمایندگان",
        "🏠 منوی اصلی",
        "پشتیبانی:",
        "تیکت:",
        "تیکت‌ها:",
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
    """Present one content panel with inline markup — no reply-chrome follow-up.

    When ``message`` is bot-owned (typical ``callback.message``), **edit in place**
    so every inline tap updates the same bubble. User-authored messages (reply
    keyboard / first open) still get a single ``answer``.
    """
    if is_bot_panel_message(message):
        if await safe_edit_inline(message, text, reply_markup=inline, **send_kw):
            return message
        # Edit failed (deleted/too old) — fall through to one new panel.
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


def is_bot_panel_message(message: Message | None) -> bool:
    """True when ``message`` is a bot-owned chat message (e.g. callback.message)."""
    if message is None:
        return False
    fu = getattr(message, "from_user", None)
    if fu is None:
        return False
    # Strict True — MagicMock.is_bot must not count as bot-owned.
    return getattr(fu, "is_bot", False) is True


async def remember_nav_panel(state: FSMContext | None, message: Message | None) -> None:
    if state is None or message is None:
        return
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    msg_id = getattr(message, "message_id", None)
    if chat_id is None or msg_id is None:
        return
    await state.update_data(
        **{NAV_PANEL_CHAT_KEY: int(chat_id), NAV_PANEL_MSG_KEY: int(msg_id)}
    )


async def clear_nav_panel(state: FSMContext | None) -> None:
    if state is None:
        return
    await state.update_data(**{NAV_PANEL_CHAT_KEY: None, NAV_PANEL_MSG_KEY: None})


def panel_is_still_live(message: Message, tracked_msg_id: int | None) -> bool:
    """True when the tracked panel is still the bottom content bubble.

    Telegram assigns increasing ``message_id`` values. A reply-keyboard tap
    is always one id after the previous message, so we allow
    ``cur <= tracked + 1`` (the tap itself). Any *intervening* message
    (bot notice, preview chrome, another user line) makes ``cur`` larger
    — editing then would bury the result; callers must resend at the
    bottom instead.

    Inline taps on the panel bubble use ``is_bot_panel_message`` and edit
    that message directly; they do not depend on this helper.
    """
    if tracked_msg_id is None:
        return False
    cur = getattr(message, "message_id", None)
    if cur is None:
        return False
    try:
        # tracked: panel; tracked+1: immediate reply-KB tap with nothing else.
        return int(cur) <= int(tracked_msg_id) + 1
    except (TypeError, ValueError):
        return False


async def present_nav_panel(
    message: Message,
    *,
    text: str,
    inline: InlineKeyboardMarkup | None,
    state: FSMContext | None = None,
    prefer_edit: bool = True,
    **send_kw: Any,
) -> Message | None:
    """One live panel: edit when acting on that bubble; else answer at bottom.

    - Bot-owned ``callback.message`` → always edit that message (same-bubble action).
    - Reply-keyboard / later user message → edit tracked panel only while
      ``panel_is_still_live`` (immediate tap, no intervening chat); otherwise
      clear the tracker and send a fresh panel at the bottom.
    """
    if prefer_edit and is_bot_panel_message(message):
        if await safe_edit_inline(message, text, reply_markup=inline, **send_kw):
            await remember_nav_panel(state, message)
            return message
    if prefer_edit and state is not None and message.bot is not None:
        data = await state.get_data()
        chat_id = data.get(NAV_PANEL_CHAT_KEY)
        msg_id = data.get(NAV_PANEL_MSG_KEY)
        cur_chat = getattr(getattr(message, "chat", None), "id", None)
        if (
            chat_id
            and msg_id
            and cur_chat is not None
            and int(chat_id) == int(cur_chat)
            and panel_is_still_live(message, int(msg_id))
        ):
            try:
                await message.bot.edit_message_text(
                    text,
                    chat_id=int(chat_id),
                    message_id=int(msg_id),
                    reply_markup=inline,
                    **send_kw,
                )
                return None
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    return None
            except Exception:
                logger.info("present_nav_panel tracked edit failed", exc_info=True)
        elif msg_id and not panel_is_still_live(message, int(msg_id) if msg_id else None):
            # Stale tracked panel — drop it so we do not keep editing history.
            await clear_nav_panel(state)
    # Force a real send even if message is bot-owned (edit already failed above).
    try:
        sent = await message.answer(text, reply_markup=inline, **send_kw)
    except Exception:
        logger.warning("present_nav_panel answer failed", exc_info=True)
        sent = None
    await remember_nav_panel(state, sent)
    return sent


def with_inline_back(
    markup: InlineKeyboardMarkup | None,
    ui: dict | None,
    back_callback: str,
) -> InlineKeyboardMarkup:
    """Append a single Back row unless the same callback is already last."""
    rows: list[list[InlineKeyboardButton]] = (
        [list(r) for r in markup.inline_keyboard] if markup else []
    )
    if rows:
        last = rows[-1]
        if any(getattr(b, "callback_data", None) == back_callback for b in last):
            return InlineKeyboardMarkup(inline_keyboard=rows)
    rows.append(_hub_back_row(ui, callback_data=back_callback))
    return InlineKeyboardMarkup(inline_keyboard=rows)


DEFAULT_TOPUP_PRESETS: tuple[int, ...] = (50_000, 100_000, 200_000, 500_000)


def parse_topup_presets(ui: dict | None = None) -> list[int]:
    """CSV of toman amounts from ``wallet_topup_presets`` (falls back to defaults)."""
    raw = str((ui or {}).get("wallet_topup_presets") or "").strip()
    out: list[int] = []
    if raw:
        for part in raw.replace("،", ",").split(","):
            part = part.replace(",", "").replace("٬", "").strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if n >= 1000:
                out.append(n)
    return out or list(DEFAULT_TOPUP_PRESETS)


def wallet_topup_presets_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Preset amounts + custom + back (Wave C)."""
    from app.services.formatting import format_toman

    presets = parse_topup_presets(ui)
    buttons = [
        _ikb(
            format_toman(n),
            callback_data=f"nv:w:amt:{n}",
            ui=ui,
            style=_style(ui, "wallet_topup", fallback="primary"),
        )
        for n in presets[:8]
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append(
        [
            _ikb(
                "✍ مبلغ دیگر",
                callback_data="nv:w:amt:custom",
                ui=ui,
                style=_style(ui, "wallet_topup", fallback="primary"),
            )
        ]
    )
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="nv:w:home",
                ui=ui,
                label_key="btn_back",
                style=_style(ui, "back"),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_methods_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Inline top-up pay methods using existing ``wtop:*`` callbacks.

    Built without importing ``reply_keyboards`` (avoids circular import via
    ``keyboards`` ↔ ``reply_keyboards``).
    """
    from app.services.users import on

    # flag, label_key, callback, style_key — same set as _topup_method_entries
    methods = (
        ("pay_card_enabled", "btn_pay_card", "wtop:card", "topup_card"),
        ("pay_gateway_enabled", "btn_pay_gateway", "wtop:gateway", "topup_gateway"),
        ("pay_psp_enabled", "btn_pay_psp", "wtop:psp", "topup_psp"),
        ("pay_crypto_enabled", "btn_pay_crypto", "wtop:crypto", "topup_crypto"),
    )
    rows: list[list[InlineKeyboardButton]] = []
    for flag, label_key, cb, style_key in methods:
        if not on(_t(ui, flag)):
            continue
        rows.append(
            [
                _ikb(
                    _t(ui, label_key),
                    callback_data=cb,
                    ui=ui,
                    label_key=label_key,
                    style=_style(ui, style_key, fallback="primary"),
                )
            ]
        )
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="nv:w:topup",
                ui=ui,
                label_key="btn_back",
                style=_style(ui, "back"),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def _hub_back_row(
    ui: dict | None, *, callback_data: str
) -> list[InlineKeyboardButton]:
    return [
        _ikb(
            _t(ui, "btn_back") or "⬅️ بازگشت",
            callback_data=callback_data,
            ui=ui,
            label_key="btn_back",
            style=_style(ui, "back"),
        )
    ]


def _pack_hub_keyboard(
    buttons: list[InlineKeyboardButton],
    ui: dict | None,
    *,
    back_callback: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append(_hub_back_row(ui, callback_data=back_callback))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_groups_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Top-level admin groups (Wave D) — mirrors ``_reply_admin_hub_entries``."""
    from app.bot.reply_keyboards import _reply_admin_hub_entries

    cb_map = {
        "adm_hub_ops": "nv:adm:ops",
        "adm_hub_people": "nv:adm:people",
        "adm_hub_product": "nv:adm:product",
        "adm_hub_system": "nv:adm:system",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _reply_admin_hub_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="menu:home")


def admin_ops_hub_keyboard(
    ui: dict | None = None,
    *,
    pending_payments: int = 0,
    pending_tickets: int = 0,
) -> InlineKeyboardMarkup:
    """Daily ops — leaf buttons reuse existing ``adm:*`` callbacks."""
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADMIN_DASH,
        REPLY_ACTION_ADMIN_ORDERS,
        REPLY_ACTION_ADMIN_PAYMENTS,
        REPLY_ACTION_ADMIN_REPORTS,
        REPLY_ACTION_ADMIN_TICKETS,
        _reply_admin_ops_entries,
    )
    from app.services.admin_counters import with_badge

    cb_map = {
        REPLY_ACTION_ADMIN_DASH: "adm:dash",
        REPLY_ACTION_ADMIN_REPORTS: "adm:reports:week",
        REPLY_ACTION_ADMIN_ORDERS: "adm:orders",
        REPLY_ACTION_ADMIN_PAYMENTS: "adm:payments",
        REPLY_ACTION_ADMIN_TICKETS: "adm:tickets",
    }
    badge_for = {
        REPLY_ACTION_ADMIN_PAYMENTS: pending_payments,
        REPLY_ACTION_ADMIN_TICKETS: pending_tickets,
    }
    buttons = [
        _ikb(
            with_badge(text, badge_for.get(key, 0)),
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _reply_admin_ops_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


def admin_people_hub_keyboard(
    ui: dict | None = None, *, can_manage_representatives: bool = True
) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADMIN_LOYALTY,
        REPLY_ACTION_ADMIN_RESELLERS,
        REPLY_ACTION_ADMIN_USERS,
        _reply_admin_people_entries,
    )

    cb_map = {
        REPLY_ACTION_ADMIN_USERS: "nv:adm:users",
        REPLY_ACTION_ADMIN_RESELLERS: "nv:adm:resellers",
        REPLY_ACTION_ADMIN_LOYALTY: "nv:adm:loy",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _reply_admin_people_entries(
            ui, can_manage_representatives=can_manage_representatives
        )
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


def admin_product_hub_keyboard(
    ui: dict | None = None,
    *,
    pg_features: frozenset[str] | set[str] | None = None,
) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADMIN_PG,
        REPLY_ACTION_ADMIN_PLANS,
        _reply_admin_product_entries,
    )

    cb_map = {
        REPLY_ACTION_ADMIN_PLANS: "nv:adm:plans",
        REPLY_ACTION_ADMIN_PG: "nv:adm:pg",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _reply_admin_product_entries(ui, pg_features=pg_features)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


def admin_system_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADMIN_BACKUP,
        REPLY_ACTION_ADMIN_BROADCAST,
        REPLY_ACTION_ADMIN_PREVIEW,
        REPLY_ACTION_ADMIN_SETTINGS,
        _reply_admin_system_entries,
    )

    cb_map = {
        REPLY_ACTION_ADMIN_SETTINGS: "nv:adm:settings",
        REPLY_ACTION_ADMIN_BROADCAST: "nv:adm:broadcast",
        REPLY_ACTION_ADMIN_BACKUP: "nv:adm:backup",
        REPLY_ACTION_ADMIN_PREVIEW: "nv:adm:preview",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _reply_admin_system_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


def pg_hub_keyboard(
    ui: dict | None = None,
    *,
    features: frozenset[str] | set[str] | None = None,
    can_create_user: bool = True,
) -> InlineKeyboardMarkup:
    """PG feature hub — leaf buttons reuse ``adm:pg:*``."""
    from app.bot.reply_keyboards import (
        REPLY_ACTION_PG_CREATE,
        REPLY_ACTION_PG_GROUP,
        REPLY_ACTION_PG_NODES,
        REPLY_ACTION_PG_SEARCH,
        REPLY_ACTION_PG_STATS,
        REPLY_ACTION_PG_TEMPLATE,
        REPLY_ACTION_PG_USERS,
        _pg_submenu_entries,
    )

    cb_map = {
        REPLY_ACTION_PG_STATS: "adm:pg:stats",
        REPLY_ACTION_PG_USERS: "adm:pg:users",
        REPLY_ACTION_PG_CREATE: "adm:pg:create",
        REPLY_ACTION_PG_SEARCH: "adm:pg:search",
        REPLY_ACTION_PG_NODES: "adm:pg:nodes",
        REPLY_ACTION_PG_GROUP: "adm:pg:group",
        REPLY_ACTION_PG_TEMPLATE: "adm:pg:template",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _pg_submenu_entries(
            ui, features=features, can_create_user=can_create_user
        )
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:product")


def admin_loyalty_manage_hub_keyboard(
    ui: dict | None = None,
    *,
    include_tiers: bool = True,
    back_callback: str = "nv:adm:people",
) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_LOY_OVERVIEW,
        REPLY_ACTION_ADM_LOY_REF_TEXT,
        REPLY_ACTION_ADM_LOY_REWARDS,
        REPLY_ACTION_ADM_LOY_RULES,
        REPLY_ACTION_ADM_LOY_SETTINGS,
        REPLY_ACTION_ADM_LOY_TIERS,
        _admin_loyalty_submenu_entries,
    )

    cb_map = {
        REPLY_ACTION_ADM_LOY_OVERVIEW: "nv:adm:loy:overview",
        REPLY_ACTION_ADM_LOY_RULES: "nv:adm:loy:rules",
        REPLY_ACTION_ADM_LOY_REWARDS: "nv:adm:loy:rewards",
        REPLY_ACTION_ADM_LOY_TIERS: "nv:adm:loy:tiers",
        REPLY_ACTION_ADM_LOY_SETTINGS: "nv:adm:loy:settings",
        REPLY_ACTION_ADM_LOY_REF_TEXT: "nv:adm:loy:reftext",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_loyalty_submenu_entries(
            ui, include_tiers=include_tiers
        )
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback=back_callback)


def reseller_manage_hub_keyboard(
    profile,
    ui: dict | None = None,
    *,
    can_add_representative: bool = False,
    pending_payments: int = 0,
    pending_tickets: int = 0,
) -> InlineKeyboardMarkup:
    """Reseller shop-bot manage hub (Wave D) — ``nv:res:ra:<action>``."""
    from app.bot.reply_keyboards import _reseller_submenu_entries
    from app.services.admin_counters import with_badge

    entries = _reseller_submenu_entries(
        profile, can_add_representative=can_add_representative
    )
    badge_for = {
        "res_payments": pending_payments,
        "res_tickets": pending_tickets,
    }
    buttons = [
        _ikb(
            with_badge(text, badge_for.get(key, 0)),
            callback_data=f"nv:res:ra:{key}",
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in entries
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="menu:home")


# ── Wave E: admin / reseller leaf hubs ─────────────────────────────────────


def admin_users_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_USERS_LIST,
        REPLY_ACTION_ADM_USERS_SEARCH,
        REPLY_ACTION_ADM_USERS_WEB,
        _admin_users_submenu_entries,
    )

    cb_map = {
        REPLY_ACTION_ADM_USERS_LIST: "adm:users:list:0",
        REPLY_ACTION_ADM_USERS_SEARCH: "adm:users:search",
        REPLY_ACTION_ADM_USERS_WEB: "adm:users:webhint",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_users_submenu_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:people")


def admin_resellers_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_RES_ADD,
        REPLY_ACTION_ADM_RES_APPS,
        REPLY_ACTION_ADM_RES_LIST,
        _admin_resellers_submenu_entries,
    )

    cb_map = {
        REPLY_ACTION_ADM_RES_LIST: "adm:resellers:list:0",
        REPLY_ACTION_ADM_RES_APPS: "adm:resapp:list",
        REPLY_ACTION_ADM_RES_ADD: "adm:resellers:add",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_resellers_submenu_entries(ui)
        if key in cb_map
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    # Optional Mini App shortcut (same as classic follow-up, without chrome).
    try:
        from app.bot.keyboards import miniapp_inline_keyboard

        mini = miniapp_inline_keyboard(
            None, view="ops", label="📱 مینی‌اپ · نمایندگان"
        )
        if mini and mini.inline_keyboard:
            rows.extend(mini.inline_keyboard)
    except Exception:
        pass
    rows.append(_hub_back_row(ui, callback_data="nv:adm:people"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_settings_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_ST_PANEL,
        _admin_settings_submenu_entries,
    )

    buttons: list[InlineKeyboardButton] = []
    for key, text in _admin_settings_submenu_entries(ui):
        if key == REPLY_ACTION_ADM_ST_PANEL:
            cb = "nv:adm:ra:adm_st_panel"
        elif key.startswith("adm_st_"):
            sec = key.replace("adm_st_", "", 1)
            if sec == "service":
                sec = "access"
            cb = f"adm:st:sec:{sec}"
        else:
            continue
        buttons.append(
            _ikb(
                text,
                callback_data=cb,
                ui=ui,
                style=_style(ui, key, fallback="primary"),
            )
        )
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:system")


def admin_backup_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    cb_map = {
        "backup_create": "adm:backup:create:noenv",
        "backup_create_env": "adm:backup:create:env",
        "backup_upload": "adm:backup:upload",
        "backup_refresh": "nv:adm:backup",
    }
    from app.bot.reply_keyboards import _admin_backup_submenu_entries

    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_backup_submenu_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:system")


def admin_broadcast_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _admin_broadcast_submenu_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"adm:broadcast:aud:{key.replace('bc_aud_', '', 1)}",
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_broadcast_submenu_entries(ui)
        if key.startswith("bc_aud_")
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:system")


def admin_plans_audience_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_PLANS_ADDONS,
        REPLY_ACTION_ADM_PLANS_AUD_RESELLERS,
        REPLY_ACTION_ADM_PLANS_AUD_USERS,
        REPLY_ACTION_ADM_PLANS_CATEGORIES,
        _admin_plans_audience_entries,
    )

    cb_map = {
        REPLY_ACTION_ADM_PLANS_AUD_USERS: "nv:adm:plans:aud:users",
        REPLY_ACTION_ADM_PLANS_AUD_RESELLERS: "nv:adm:plans:aud:resellers",
        REPLY_ACTION_ADM_PLANS_CATEGORIES: "nv:adm:ra:adm_plans_categories",
        REPLY_ACTION_ADM_PLANS_ADDONS: "nv:adm:ra:adm_plans_addons",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_plans_audience_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:product")


def admin_plans_kind_hub_keyboard(
    audience: str, ui: dict | None = None
) -> InlineKeyboardMarkup:
    """List-screen actions after choosing users/resellers audience."""
    _ = audience
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_PLANS_ADD,
        REPLY_ACTION_ADM_PLANS_ADDONS,
        REPLY_ACTION_ADM_PLANS_CATEGORIES,
        _admin_plans_list_entries,
    )

    cb_map = {
        REPLY_ACTION_ADM_PLANS_ADD: "nv:adm:ra:adm_plans_add",
        REPLY_ACTION_ADM_PLANS_CATEGORIES: "nv:adm:ra:adm_plans_categories",
        REPLY_ACTION_ADM_PLANS_ADDONS: "nv:adm:ra:adm_plans_addons",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_plans_list_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:plans")


def admin_plans_add_type_hub_keyboard(
    audience: str, ui: dict | None = None
) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _admin_plans_add_type_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"nv:adm:ra:{key}",
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _admin_plans_add_type_entries(audience, ui)
    ]
    back = (
        "nv:adm:plans:aud:resellers"
        if audience == "resellers"
        else "nv:adm:plans:aud:users"
    )
    return _pack_hub_keyboard(buttons, ui, back_callback=back)


def reseller_settings_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _reseller_settings_submenu_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"nv:res:ra:{key}",
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _reseller_settings_submenu_entries(ui)
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:res:home")


def reseller_plans_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _reseller_plans_submenu_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"nv:res:ra:{key}",
            ui=ui,
            style=_style(ui, key, fallback="primary"),
        )
        for key, text in _reseller_plans_submenu_entries(ui)
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:res:home")


def service_card_keyboard(
    service_id: int, ui: dict | None = None
) -> InlineKeyboardMarkup:
    """Inline actions for one service card (replaces service_actions_reply_keyboard).

    Reuses existing ``svc:*`` / ``guide:svc:*`` callbacks — no duplicated business logic.
    Cancel sits above delete so destructive delete stays visually separated.
    """
    sid = int(service_id)
    rows: list[list[InlineKeyboardButton]] = [
        [
            _ikb(
                _t(ui, "btn_renew") or "🔄 تمدید",
                callback_data=f"svc:renew:{sid}",
                ui=ui,
                label_key="btn_renew",
                style=_style(ui, "svc_renew", fallback="primary"),
            ),
            _ikb(
                _t(ui, "btn_svc_addon") or "➕ حجم / زمان",
                callback_data=f"svc:addon:{sid}",
                ui=ui,
                label_key="btn_svc_addon",
                style=_style(ui, "svc_addon", fallback="primary"),
            ),
        ],
        [
            _ikb(
                _t(ui, "btn_sub_link") or "🔗 لینک و QR",
                callback_data=f"svc:link:{sid}",
                ui=ui,
                label_key="btn_sub_link",
                style=_style(ui, "svc_link"),
            ),
            _ikb(
                _t(ui, "btn_guides") or _t(ui, "btn_guide") or "📘 آموزش اتصال",
                callback_data=f"guide:svc:{sid}",
                ui=ui,
                label_key="btn_guides",
            ),
        ],
        [
            _ikb(
                "♻️ رفرش وضعیت",
                callback_data=f"svc:view:{sid}",
                ui=ui,
                style=_style(ui, "svc_refresh"),
            ),
            _ikb(
                "⚙️ تنظیمات خودکار",
                callback_data=f"svc:auto:{sid}",
                ui=ui,
            ),
        ],
        [
            _ikb(
                "📝 درخواست لغو سرویس",
                callback_data=f"svc:cancel:{sid}",
                ui=ui,
            ),
        ],
        [
            _ikb(
                "🗑 حذف سرویس",
                callback_data=f"svc:delask:{sid}",
                ui=ui,
                style=_style(ui, "svc_delete", fallback="danger"),
            ),
        ],
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="svc:list",
                ui=ui,
                label_key="btn_back",
                style=_style(ui, "back"),
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_list_keyboard(
    services: list, ui: dict | None = None
) -> InlineKeyboardMarkup:
    """Paginated-ready service list with back-to-home (inline nav)."""
    from app.bot.keyboards import services_keyboard

    base = services_keyboard(services, ui)
    rows = list(base.inline_keyboard)
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
