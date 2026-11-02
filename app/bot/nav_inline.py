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

from app.bot.chat_cleanup import note_throwaway, sweep_above
from app.bot.edit_mode import editable_screen
from app.bot.keyboards import _ikb, _t
from app.bot.list_pages import Page, page_of, pager_row
from app.bot.reseller_hub_groups import (
    RESELLER_GROUPS,
    RESELLER_HOME_CALLBACK,
    back_callback_for,
    group_entries,
    reseller_group,
)
from app.services.wallet_topup import DEFAULT_TOPUP_PRESETS, parse_topup_presets  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

SERVICES_PAGE_SIZE = 10
SERVICES_LIST_CALLBACK = "svc:list"
SERVICES_SEARCH_CALLBACK = "svc:find"

SEARCH_SERVICES_TEXT = "🔍 جستجوی سرویس"

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
            await sweep_above(message)
            return message
        # Edit failed (deleted/too old) — fall through to one new panel.
    try:
        sent = await message.answer(text, reply_markup=inline, **send_kw)
    except Exception:
        logger.warning("present_inline_only failed", exc_info=True)
        return None
    await _show_as_the_panel(sent, inline)
    return sent


async def _show_as_the_panel(sent: Message | None, markup: Any) -> None:
    """A freshly sent panel is throwaway itself and clears the throwaway messages above it."""
    if sent is None:
        return
    note_throwaway(sent, reply_markup=markup)
    await sweep_above(sent)


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
            await sweep_above(message)
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
                await sweep_above(message, panel_id=int(msg_id))
                return None
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    await sweep_above(message, panel_id=int(msg_id))
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
    await _show_as_the_panel(sent, inline)
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


def wallet_topup_presets_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Preset amounts + custom + back (Wave C)."""
    from app.services.formatting import format_toman

    presets = parse_topup_presets(ui)
    buttons = [
        _ikb(
            format_toman(n),
            callback_data=f"nv:w:amt:{n}",
            ui=ui,
            style_id="wallet_topup", style_fallback="primary",
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
                style_id="wallet_topup", style_fallback="primary",
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
                style_id="back",
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
        ("pay_bbc_enabled", "btn_pay_bbc", "wtop:bbc", "topup_bbc"),
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
                    style_id=style_key, style_fallback="primary",
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
                style_id="back",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@editable_screen("c.wallet")
def wallet_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            _ikb(
                _t(ui, "btn_wallet_topup") or "➕ شارژ",
                callback_data="nv:w:topup",
                ui=ui,
                label_key="btn_wallet_topup",
                style_id="wallet_topup", style_fallback="primary",
            ),
            _ikb(
                _t(ui, "btn_wallet_tx") or "📜 تراکنش‌ها",
                callback_data="nv:w:tx",
                ui=ui,
                label_key="btn_wallet_tx",
                style_id="wallet_tx",
            ),
        ],
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="menu:home",
                ui=ui,
                label_key="btn_back",
                style_id="back",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@editable_screen("c.support")
def support_hub_keyboard(
    ui: dict | None = None,
    *,
    contact_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            _ikb(
                _t(ui, "btn_support_new") or "✉️ تیکت جدید",
                callback_data="nv:s:new",
                ui=ui,
                label_key="btn_support_new",
                style_id="support_new", style_fallback="primary",
            ),
            _ikb(
                _t(ui, "btn_support_list") or "📋 تیکت‌های من",
                callback_data="nv:s:list",
                ui=ui,
                label_key="btn_support_list",
                style_id="support_list",
            ),
        ]
    ]
    if contact_rows:
        rows.extend(contact_rows)
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="menu:home",
                ui=ui,
                label_key="btn_back",
                style_id="back",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@editable_screen("c.loyalty")
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
            action=key,
            style_id=style_map[key], style_fallback="primary",
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
                style_id="back",
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
            style_id="back",
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


def queue_shortcut_row(
    ui: dict | None,
    *,
    payments: int = 0,
    tickets: int = 0,
    cancellations: int = 0,
    deliveries: int = 0,
    payments_cb: str = "adm:payments",
    tickets_cb: str = "adm:tickets",
    cancellations_cb: str = "adm:cancellations",
    deliveries_cb: str = "adm:orders",
) -> list[InlineKeyboardButton]:
    """Non-zero queue counts → danger shortcuts (side by side)."""
    from app.services.admin_counters import with_badge

    row: list[InlineKeyboardButton] = []
    if int(payments or 0) > 0:
        row.append(
            _ikb(
                with_badge("🧾 رسید", payments),
                callback_data=payments_cb,
                ui=ui,
                style_id="reject", style_fallback="danger",
            )
        )
    if int(tickets or 0) > 0:
        row.append(
            _ikb(
                with_badge("🎫 تیکت", tickets),
                callback_data=tickets_cb,
                ui=ui,
                style_id="reject", style_fallback="danger",
            )
        )
    if int(cancellations or 0) > 0:
        row.append(
            _ikb(
                with_badge("📝 لغو", cancellations),
                callback_data=cancellations_cb,
                ui=ui,
                style_id="reject", style_fallback="danger",
            )
        )
    if int(deliveries or 0) > 0:
        row.append(
            _ikb(
                with_badge("⚠️ تحویل", deliveries),
                callback_data=deliveries_cb,
                ui=ui,
                style_id="reject", style_fallback="danger",
            )
        )
    return row


@editable_screen("a.home")
def admin_groups_hub_keyboard(
    ui: dict | None = None,
    *,
    pending_payments: int = 0,
    pending_tickets: int = 0,
    pending_cancellations: int = 0,
    pending_deliveries: int = 0,
) -> InlineKeyboardMarkup:
    """Top-level admin groups (Wave D) — mirrors ``_reply_admin_hub_entries``.

    Non-zero queue counts become a danger shortcut row above the groups, and the daily-operations
    group carries their sum so open work is visible before opening it.
    """
    from app.bot.reply_keyboards import _reply_admin_hub_entries
    from app.services.admin_counters import with_badge

    open_work = pending_payments + pending_tickets + pending_cancellations + pending_deliveries

    cb_map = {
        "adm_hub_ops": "nv:adm:ops",
        "adm_hub_people": "nv:adm:people",
        "adm_hub_product": "nv:adm:product",
        "adm_hub_marketing": "nv:adm:marketing",
        "adm_hub_system": "nv:adm:system",
        "connection_status_admin": "cma:home:0",
    }
    buttons = [
        _ikb(
            with_badge(text, open_work) if key == "adm_hub_ops" else text,
            callback_data=cb_map[key],
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reply_admin_hub_entries(ui)
        if key in cb_map
    ]
    rows: list[list[InlineKeyboardButton]] = []
    shortcuts = queue_shortcut_row(
        ui,
        payments=pending_payments,
        tickets=pending_tickets,
        cancellations=pending_cancellations,
        deliveries=pending_deliveries,
    )
    if shortcuts:
        rows.append(shortcuts)
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append(
        [
            _ikb(
                _t(ui, "btn_adm_edit_mode") or "✏️ حالت ویرایش منوها",
                callback_data="ed:a:h",
                ui=ui,
                label_key="btn_adm_edit_mode",
                style_id="adm_edit_mode",
            )
        ]
    )
    rows.append(_hub_back_row(ui, callback_data="menu:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


@editable_screen("a.ops")
def admin_ops_hub_keyboard(
    ui: dict | None = None,
    *,
    pending_payments: int = 0,
    pending_tickets: int = 0,
    pending_deliveries: int = 0,
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
        REPLY_ACTION_ADMIN_ORDERS: pending_deliveries,
    }
    buttons = [
        _ikb(
            with_badge(text, badge_for.get(key, 0)),
            callback_data=cb_map[key],
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reply_admin_ops_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


@editable_screen("a.people")
def admin_people_hub_keyboard(
    ui: dict | None = None, *, can_manage_representatives: bool = True
) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADMIN_RESELLERS,
        REPLY_ACTION_ADMIN_USERS,
        _reply_admin_people_entries,
    )

    cb_map = {
        REPLY_ACTION_ADMIN_USERS: "nv:adm:users",
        REPLY_ACTION_ADMIN_RESELLERS: "nv:adm:resellers",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reply_admin_people_entries(
            ui, can_manage_representatives=can_manage_representatives
        )
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


@editable_screen("a.product")
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
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reply_admin_product_entries(ui, pg_features=pg_features)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


@editable_screen("a.marketing")
def admin_marketing_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    """Club and broadcast leaves reuse ``nv:adm:*``; gift codes and campaigns are inline-only."""
    from app.bot.marketing_panels import CAMPAIGN_PANEL_CALLBACK, GIFT_PANEL_CALLBACK

    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADMIN_BROADCAST,
        REPLY_ACTION_ADMIN_LOYALTY,
        _reply_admin_marketing_entries,
    )

    cb_map = {
        REPLY_ACTION_ADMIN_LOYALTY: "nv:adm:loy",
        REPLY_ACTION_ADMIN_BROADCAST: "nv:adm:broadcast",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reply_admin_marketing_entries(ui)
        if key in cb_map
    ]
    buttons.extend(
        [
            _ikb(
                _t(ui, "btn_adm_gift_codes"),
                callback_data=GIFT_PANEL_CALLBACK,
                ui=ui,
                label_key="btn_adm_gift_codes",
                style_id="adm_gift_codes",
            ),
            _ikb(
                _t(ui, "btn_adm_campaigns"),
                callback_data=CAMPAIGN_PANEL_CALLBACK,
                ui=ui,
                label_key="btn_adm_campaigns",
                style_id="adm_campaigns",
            ),
        ]
    )
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


@editable_screen("a.system")
def admin_system_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADMIN_BACKUP,
        REPLY_ACTION_ADMIN_PREVIEW,
        REPLY_ACTION_ADMIN_SETTINGS,
        _reply_admin_system_entries,
    )

    cb_map = {
        REPLY_ACTION_ADMIN_SETTINGS: "nv:adm:settings",
        REPLY_ACTION_ADMIN_BACKUP: "nv:adm:backup",
        REPLY_ACTION_ADMIN_PREVIEW: "nv:adm:preview",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reply_admin_system_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:home")


@editable_screen("a.pg")
def pg_hub_keyboard(
    ui: dict | None = None,
    *,
    features: frozenset[str] | set[str] | None = None,
    can_create_user: bool = True,
    back_callback: str = "nv:adm:product",
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
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _pg_submenu_entries(
            ui, features=features, can_create_user=can_create_user
        )
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback=back_callback)


@editable_screen("a.loyalty")
def admin_loyalty_manage_hub_keyboard(
    ui: dict | None = None,
    *,
    include_tiers: bool = True,
    back_callback: str = "nv:adm:marketing",
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
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _admin_loyalty_submenu_entries(
            ui, include_tiers=include_tiers
        )
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback=back_callback)


def _reseller_entry_buttons(
    entries: list[tuple[str, str]], ui: dict | None, badge_for: dict[str, int]
) -> list[InlineKeyboardButton]:
    from app.services.admin_counters import with_badge

    return [
        _ikb(
            with_badge(text, badge_for.get(key, 0)),
            callback_data=f"nv:res:ra:{key}",
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in entries
    ]


@editable_screen("r.home")
def reseller_manage_hub_keyboard(
    profile,
    ui: dict | None = None,
    *,
    can_add_representative: bool = False,
    pending_payments: int = 0,
    pending_tickets: int = 0,
    pending_cancellations: int = 0,
    pending_deliveries: int = 0,
) -> InlineKeyboardMarkup:
    """Shop panel home: queue shortcuts, then one button per group that has an entry."""
    from app.bot.reply_keyboards import _reseller_submenu_entries
    from app.services.admin_counters import with_badge

    entries = _reseller_submenu_entries(
        profile, can_add_representative=can_add_representative, ui=ui
    )
    group_badge = {"daily": pending_payments + pending_tickets + pending_deliveries}
    buttons = [
        _ikb(
            with_badge(_t(ui, group.label_key), group_badge.get(group.id, 0)),
            callback_data=group.callback,
            ui=ui,
            label_key=group.label_key,
            style_id=group.label_key.removeprefix("btn_"), style_fallback="primary",
        )
        for group in RESELLER_GROUPS
        if group_entries(group, entries)
    ]
    rows: list[list[InlineKeyboardButton]] = []
    shortcuts = queue_shortcut_row(
        ui,
        payments=pending_payments,
        tickets=pending_tickets,
        cancellations=pending_cancellations,
        deliveries=pending_deliveries,
        payments_cb="res:payments",
        tickets_cb="res:tickets",
        cancellations_cb="res:cancellations",
        deliveries_cb="res:orders",
    )
    if shortcuts:
        rows.append(shortcuts)
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append(_hub_back_row(ui, callback_data="menu:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


@editable_screen(lambda group_id, *_a, **_kw: f"r.{group_id}")
def reseller_group_hub_keyboard(
    group_id: str,
    profile,
    ui: dict | None = None,
    *,
    can_add_representative: bool = False,
    pending_payments: int = 0,
    pending_tickets: int = 0,
    pending_deliveries: int = 0,
) -> InlineKeyboardMarkup:
    """One shop panel group; «بازگشت» returns to the panel home."""
    from app.bot.reply_keyboards import _reseller_submenu_entries

    group = reseller_group(group_id)
    entries = _reseller_submenu_entries(
        profile, can_add_representative=can_add_representative, ui=ui
    )
    shown = group_entries(group, entries) if group else []
    badge_for = {"res_payments": pending_payments, "res_tickets": pending_tickets, "res_orders": pending_deliveries}
    return _pack_hub_keyboard(
        _reseller_entry_buttons(shown, ui, badge_for), ui, back_callback=RESELLER_HOME_CALLBACK
    )


def reseller_access_card_keyboard(
    profile, ui: dict | None = None
) -> InlineKeyboardMarkup:
    """Links under the access card plus «بازگشت» to the reseller hub."""
    from app.services.pg_admin_subscription import is_subscription_plan

    rows: list[list[InlineKeyboardButton]] = []
    if profile.bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"باز کردن @{profile.bot_username}",
                    url=f"https://t.me/{profile.bot_username}",
                )
            ]
        )
    plan = getattr(profile, "plan", None)
    if plan is not None and is_subscription_plan(plan):
        rows.append(
            [InlineKeyboardButton(text="🔄 تمدید سرویس", callback_data="res:renew")]
        )
    rows.append(_hub_back_row(ui, callback_data="nv:res:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reseller_main_bot_hub_keyboard(
    profile,
    ui: dict | None = None,
    *,
    show_pg: bool = False,
) -> InlineKeyboardMarkup:
    """Shop owner hub on the platform bot: access card, PasarGuard and capacity."""
    from app.bot.keyboards import REPLY_ACTION_ADMIN_PG, REPLY_ACTION_CREDS
    from app.bot.reply_keyboards import _reseller_capacity_entries

    buttons = [
        _ikb(
            _t(ui, "btn_reseller_creds") or "🔑 اطلاعات ورود",
            callback_data=f"nv:res:ra:{REPLY_ACTION_CREDS}",
            ui=ui,
            action=REPLY_ACTION_CREDS,
            style_id=REPLY_ACTION_CREDS, style_fallback="primary",
        )
    ]
    if show_pg:
        buttons.append(
            _ikb(
                _t(ui, "btn_adm_pg"),
                callback_data="nv:adm:pg",
                ui=ui,
                action=REPLY_ACTION_ADMIN_PG,
                style_id=REPLY_ACTION_ADMIN_PG, style_fallback="primary",
            )
        )
    buttons.extend(
        _ikb(
            text,
            callback_data=f"nv:res:ra:{key}",
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reseller_capacity_entries(profile, ui)
    )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(_hub_back_row(ui, callback_data="menu:home"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Wave E: admin / reseller leaf hubs ─────────────────────────────────────


@editable_screen("a.users")
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
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _admin_users_submenu_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:people")


@editable_screen("a.resellers")
def admin_resellers_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_RES_ADD,
        REPLY_ACTION_ADM_RES_APPS,
        REPLY_ACTION_ADM_RES_LIST,
        REPLY_ACTION_ADM_RES_SEARCH,
        _admin_resellers_submenu_entries,
    )

    cb_map = {
        REPLY_ACTION_ADM_RES_LIST: "adm:resellers:list:0",
        REPLY_ACTION_ADM_RES_SEARCH: "adm:resellers:search",
        REPLY_ACTION_ADM_RES_APPS: "adm:resapp:list",
        REPLY_ACTION_ADM_RES_ADD: "adm:resellers:add",
    }
    buttons = [
        _ikb(
            text,
            callback_data=cb_map[key],
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _admin_resellers_submenu_entries(ui)
        if key in cb_map
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.append(_hub_back_row(ui, callback_data="nv:adm:people"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


@editable_screen("a.settings")
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
                action=key,
                style_id=key, style_fallback="primary",
            )
        )
    markup = _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:system")
    search_row = [
        _ikb(
            _t(ui, "btn_adm_st_find"),
            callback_data="adm:st:find",
            ui=ui,
            label_key="btn_adm_st_find",
            style_id="adm_st_find",
        )
    ]
    markup.inline_keyboard.insert(0, search_row)
    return markup


@editable_screen("a.backup")
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
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _admin_backup_submenu_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:system")


@editable_screen("a.broadcast")
def admin_broadcast_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _admin_broadcast_submenu_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"adm:broadcast:aud:{key.replace('bc_aud_', '', 1)}",
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _admin_broadcast_submenu_entries(ui)
        if key.startswith("bc_aud_")
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:marketing")


@editable_screen("a.plans")
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
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _admin_plans_audience_entries(ui)
        if key in cb_map
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback="nv:adm:product")



def _plans_kind_screen_id(audience: str, *_args: object, **_kwargs: object) -> str:
    return f"a.plans.{audience}"


@editable_screen(_plans_kind_screen_id)
def admin_plans_audience_keyboard(
    audience: str,
    ui: dict | None = None,
    *,
    plan_rows: list[list[InlineKeyboardButton]] | None = None,
    pager: list[InlineKeyboardButton] | None = None,
) -> InlineKeyboardMarkup:
    """One audience's plans: add / catalog actions, a page of plan rows, «بازگشت» to the plans hub."""
    from app.bot.reply_keyboards import (
        REPLY_ACTION_ADM_PLANS_ADD,
        REPLY_ACTION_ADM_PLANS_ADDONS,
        REPLY_ACTION_ADM_PLANS_CATEGORIES,
        _admin_plans_list_entries,
    )

    _ = audience  # names the screen for the menu editor
    cb_map = {
        REPLY_ACTION_ADM_PLANS_ADD: "nv:adm:ra:adm_plans_add",
        REPLY_ACTION_ADM_PLANS_CATEGORIES: "nv:adm:ra:adm_plans_categories",
        REPLY_ACTION_ADM_PLANS_ADDONS: "nv:adm:ra:adm_plans_addons",
    }
    actions = [
        _ikb(text, callback_data=cb_map[key], ui=ui, action=key, style_id=key, style_fallback="primary")
        for key, text in _admin_plans_list_entries(ui)
        if key in cb_map
    ]
    rows = [actions[i : i + 2] for i in range(0, len(actions), 2)]
    rows.extend(plan_rows or [])
    if pager:
        rows.append(pager)
    rows.append(_hub_back_row(ui, callback_data="nv:adm:plans"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_plans_add_type_hub_keyboard(
    audience: str, ui: dict | None = None
) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _admin_plans_add_type_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"nv:adm:ra:{key}",
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _admin_plans_add_type_entries(audience, ui)
    ]
    back = (
        "nv:adm:plans:aud:resellers"
        if audience == "resellers"
        else "nv:adm:plans:aud:users"
    )
    return _pack_hub_keyboard(buttons, ui, back_callback=back)


@editable_screen("r.settings")
def reseller_settings_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _reseller_settings_submenu_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"nv:res:ra:{key}",
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reseller_settings_submenu_entries(ui)
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback=back_callback_for("res_settings"))


@editable_screen("r.plans")
def reseller_plans_hub_keyboard(ui: dict | None = None) -> InlineKeyboardMarkup:
    from app.bot.reply_keyboards import _reseller_plans_submenu_entries

    buttons = [
        _ikb(
            text,
            callback_data=f"nv:res:ra:{key}",
            ui=ui,
            action=key,
            style_id=key, style_fallback="primary",
        )
        for key, text in _reseller_plans_submenu_entries(ui)
    ]
    return _pack_hub_keyboard(buttons, ui, back_callback=back_callback_for("res_plans"))


def _service_button(ui: dict | None, label_key: str, fallback: str, callback_data: str, style_id: str, **kw: Any):
    return _ikb(
        _t(ui, label_key) or fallback, callback_data=callback_data, ui=ui, label_key=label_key, style_id=style_id, **kw
    )


@editable_screen("c.service")
def service_card_keyboard(service_id: int, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Inline actions for one service card: the daily ones, then «⚙️ بیشتر» for the rest.

    Reuses existing ``svc:*`` / ``guide:svc:*`` callbacks — no duplicated business logic.
    """
    sid = int(service_id)
    rows: list[list[InlineKeyboardButton]] = [
        [
            _service_button(ui, "btn_renew", "🔄 تمدید", f"svc:renew:{sid}", "svc_renew", style_fallback="primary"),
            _service_button(ui, "btn_svc_addon", "➕ حجم / زمان", f"svc:addon:{sid}", "svc_addon", style_fallback="primary"),
        ],
        [
            _service_button(ui, "btn_sub_link", "🔗 لینک و QR", f"svc:link:{sid}", "svc_link"),
            _service_button(
                ui, "btn_guides", _t(ui, "btn_guide") or "📘 آموزش اتصال", f"guide:svc:{sid}", "guides",
                style_fallback="primary",
            ),
        ],
        [_service_button(ui, "btn_svc_more", "⚙️ بیشتر", f"svc:more:{sid}", "svc_more")],
        [_service_button(ui, "btn_back", "⬅️ بازگشت", SERVICES_LIST_CALLBACK, "back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@editable_screen("c.service_more")
def service_more_keyboard(
    service_id: int, ui: dict | None = None, *, external: bool = False, wireguard: bool = False
) -> InlineKeyboardMarkup:
    """«⚙️ بیشتر» of a service card: refresh, WireGuard, automation, cancellation and delete.

    The WireGuard button shows only for a service the panel gave a WireGuard address (``wireguard``).
    Cancel sits above delete so destructive delete stays visually separated. A service that was
    not sold by the bot (``external``) can never be cancelled for a refund, so it has no cancel button.
    """
    sid = int(service_id)
    rows: list[list[InlineKeyboardButton]] = [
        [_service_button(ui, "btn_svc_refresh", "♻️ رفرش وضعیت", f"svc:view:{sid}", "svc_refresh")]
    ]
    if wireguard:
        rows.append([_service_button(ui, "btn_svc_wireguard", "🔐 فایل WireGuard", f"svc:wg:{sid}", "svc_wireguard")])
    rows.append([_service_button(ui, "btn_svc_auto", "⚙️ تنظیمات خودکار", f"svc:auto:{sid}", "svc_auto")])
    if not external:
        rows.append([_service_button(ui, "btn_svc_cancel", "📝 درخواست لغو", f"svc:cancel:{sid}", "svc_cancel")])
    rows.append(
        [_service_button(ui, "btn_svc_delete", "🗑 حذف سرویس", f"svc:delask:{sid}", "svc_delete", style_fallback="danger")]
    )
    rows.append([_service_button(ui, "btn_back", "⬅️ بازگشت", f"svc:view:{sid}", "back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_page(services: list, page_index: int = 0) -> Page:
    return page_of(services, page_index, SERVICES_PAGE_SIZE)


@editable_screen("c.services")
def services_list_keyboard(
    services: list,
    ui: dict | None = None,
    *,
    reseller_capacity_label: str | None = None,
    allow_import: bool = False,
    page_index: int = 0,
) -> InlineKeyboardMarkup:
    """One page of the customer's services; a long list gets «قبلی/بعدی» and a search."""
    from app.bot.keyboards import services_keyboard
    from app.bot.service_import_ui import add_existing_row

    page = services_page(services, page_index)
    base = services_keyboard(
        page.items, ui, reseller_capacity_label=reseller_capacity_label
    )
    rows = list(base.inline_keyboard)
    if page.is_split:
        rows.append(pager_row(page, SERVICES_LIST_CALLBACK))
        rows.append(
            [InlineKeyboardButton(text=SEARCH_SERVICES_TEXT, callback_data=SERVICES_SEARCH_CALLBACK)]
        )
    if allow_import:
        rows.append(add_existing_row(ui))
    rows.append(
        [
            _ikb(
                _t(ui, "btn_back") or "⬅️ بازگشت",
                callback_data="menu:home",
                ui=ui,
                label_key="btn_back",
                style_id="back",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

