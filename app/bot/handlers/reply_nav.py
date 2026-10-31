"""Reply-keyboard main + submenu navigation (3.6.0+).

All navigation (including submenus + pay methods + shop chrome) uses the reply keyboard.
Every submenu has «بازگشت» (one level) and «منوی اصلی».
Inline under messages: plans, services/user/reseller names, approve/reject, URL contacts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import menu_nav as nav
from app.bot.menu_nav import restore_main_reply
from app.bot.reseller_hub_groups import back_callback_for
from app.bot.reply_keyboards import _reply_admin_marketing_entries
from app.db.models import BotUser, Order, ResellerProfile, Role
from app.services.formatting import format_message
from app.services.home_overview import admin_customer_counts
from app.services.service_scope import list_customer_services
from app.services.users import current_shop_reseller_id, get_all_settings
from app.services.redact import user_safe_error
from app.services import bot_feedback_texts as feedback
from app.services.screen_titles import (
    screen_caption,
    screen_heading,
    screen_title_html,
    screen_title_raw,
    with_caption,
)

logger = logging.getLogger(__name__)
router = Router(name="reply_nav")

_RESELLER_HUB_BACK = "nv:res:home"

# Prompts whose answer is a free-form message: only labels of the visible main keyboard
# (plus home / back) may interrupt them; hidden submenu labels are delivered as text.
FREE_TEXT_INPUT_STATES: frozenset[str] = frozenset(
    {
        "AdminStates:user_message",
        "AdminStates:ticket_reply",
        "AdminStates:broadcast_text",
        "AdminStates:revoke_reseller_reason",
        "AdminStates:block_user_reason",
        "AdminStates:delete_user_reason",
        "ResellerStates:user_message",
        "SupportStates:subject",
        "SupportStates:body",
        "SupportStates:reply",
        "TicketActionStates:reply",
    }
)


class _SoftCallback:
    """Duck-typed CallbackQuery so admin handlers can edit a bot-owned message."""

    def __init__(self, message: Message, data: str, *, back_callback: str | None = None):
        self.id = "reply"
        self.from_user = message.from_user
        self.message = message
        self.data = data
        self.bot = message.bot
        # A denial replaces the «⏳» bubble, so it always keeps a way out.
        self._denial_markup = InlineKeyboardMarkup(
            inline_keyboard=[[kb.back_button(None, back_callback)] if back_callback else [kb.home_button(None)]]
        )

    async def answer(self, *args, **kwargs):
        # Show denial / alert text on the ⏳ bubble (no-op answer would leave it stuck)
        text = args[0] if args else kwargs.get("text")
        if text:
            try:
                await self.message.edit_text(
                    str(text)[:500], reply_markup=self._denial_markup
                )
            except TelegramAPIError:
                logger.debug("soft callback denial edit failed", exc_info=True)
        return True


def _admin_back(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, callback_data)]])


def _hub_back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[kb.back_button(None, _RESELLER_HUB_BACK)]]
    )


async def _live_bubble(message: Message) -> Message:
    """Reuse the tapped panel when the tap came from an inline button; else a ⏳ bubble."""
    from app.bot.nav_inline import is_bot_panel_message

    if is_bot_panel_message(message):
        return message
    return await message.answer("⏳")

# Entries owning shared / colliding labels at a nav level: (ui, is_reseller_bot) -> entries.
_NAV_LEVEL_ENTRIES: dict[str, Callable[[dict, bool], list[tuple[str, str]]]] = {
    nav.NAV_TOPUP_PAY: lambda ui, _rb: kb._topup_method_entries(ui),
    nav.NAV_PAY: lambda ui, _rb: kb._pay_method_entries(ui),
    nav.NAV_SERVICE: lambda ui, _rb: kb._service_action_entries(ui),
    nav.NAV_REVIEW: lambda ui, _rb: kb._review_submenu_entries(ui),
    nav.NAV_ADMIN_BROADCAST: lambda ui, _rb: kb._admin_broadcast_submenu_entries(ui),
    nav.NAV_ADMIN_PLANS_AUDIENCE: lambda ui, _rb: kb._admin_plans_audience_entries(ui),
    nav.NAV_ADMIN_PLANS_KIND: lambda ui, _rb: kb._admin_plans_list_entries(ui),
    nav.NAV_SHOP: lambda ui, _rb: kb._shop_submenu_entries(
        ui, custom_enabled=True, wholesale_enabled=True
    ),
    # «👥 کاربران» on PG keyboard must not resolve to admin hub users
    nav.NAV_ADMIN_PG: lambda ui, _rb: kb._pg_submenu_entries(ui),
    nav.NAV_ADMIN_OPS: lambda ui, _rb: kb._reply_admin_ops_entries(ui),
    nav.NAV_ADMIN_PEOPLE: lambda ui, _rb: kb._reply_admin_people_entries(ui),
    nav.NAV_ADMIN_PRODUCT: lambda ui, _rb: kb._reply_admin_product_entries(ui),
    nav.NAV_ADMIN_MARKETING: lambda ui, _rb: _reply_admin_marketing_entries(ui),
    nav.NAV_ADMIN_SYSTEM: lambda ui, _rb: kb._reply_admin_system_entries(ui),
    nav.NAV_LOYALTY: lambda ui, _rb: kb._loyalty_submenu_entries(ui),
    nav.NAV_ADMIN_LOYALTY: lambda ui, rb: kb._admin_loyalty_submenu_entries(
        ui, include_tiers=not rb
    ),
}


async def _apply_nav_level_labels(
    mapping: dict[str, str],
    state: FSMContext,
    *,
    ui: dict,
    role: str,
    is_reseller_bot: bool,
) -> None:
    """Resolve shared / colliding labels by the current nav level."""
    level = await nav.get_nav_level(state)
    entries_for = _NAV_LEVEL_ENTRIES.get(level)
    if entries_for is not None:
        entries = entries_for(ui, is_reseller_bot)
    elif level == nav.NAV_ADMIN_PLANS_ADD_TYPE:
        aud = str((await state.get_data()).get("_adm_plans_aud") or "users")
        entries = kb._admin_plans_add_type_entries(aud, ui)
    elif role == "admin" and not is_reseller_bot:
        # Prefer 4-group hub labels on MAIN / NAV_ADMIN (avoid leaf collisions)
        entries = kb._reply_admin_hub_entries(ui)
    else:
        return
    for key, label in entries:
        mapping[(label or "").strip()] = key


async def _menu_profile(
    session: AsyncSession,
    db_user: BotUser,
    *,
    role: str,
    show_creds: bool,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> ResellerProfile | None:
    """Reseller profile whose panel labels this actor may press, or None."""
    profile = None
    if is_reseller_bot and role == Role.RESELLER.value:
        from app.services.reseller_access import load_reseller_actor

        _, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif show_creds and not is_reseller_bot:
        from app.services.resellers import get_reseller_profile

        profile = await get_reseller_profile(session, int(db_user.id))
        if profile is not None and not profile.is_active:
            profile = None
    return profile


class ReplyMenuTextFilter(BaseFilter):
    """Match known reply-menu / submenu labels (works even during FSM — clears it)."""

    async def __call__(
        self,
        message: Message,
        session: AsyncSession,
        db_user: BotUser,
        state: FSMContext,
        is_reseller_bot: bool = False,
        reseller_owner_id: int | None = None,
    ) -> bool | dict:
        text = (message.text or "").strip()
        if not text or kb.is_cancel_text(text):
            return False
        free_text = await state.get_state() in FREE_TEXT_INPUT_STATES
        ui, role, has, show_creds = await _nav_context(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        profile = await _menu_profile(
            session,
            db_user,
            role=role,
            show_creds=show_creds,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        can_add = False
        if profile is not None and is_reseller_bot:
            from app.services.representative_unification import (
                shop_bot_can_manage_representatives,
            )

            can_add = await shop_bot_can_manage_representatives(
                session,
                db_user,
                is_reseller_bot=True,
                reseller_owner_id=reseller_owner_id,
                reseller_profile_id=int(getattr(profile, "id", 0) or 0) or None,
            )
        mapping = kb.reply_action_map(
            role,
            has_services=has,
            ui=ui,
            show_reseller_creds=show_creds,
            include_submenus=not free_text,
            is_reseller_bot=is_reseller_bot,
            profile=profile,
            can_add_representative=can_add,
        )
        # Main-bot admin / shop reseller can also hit user-preview labels
        if role == "admin" and not is_reseller_bot:
            for k, v in kb.reply_action_map(
                "user",
                has_services=has,
                ui=ui,
                as_user=True,
                include_submenus=not free_text,
                is_reseller_bot=False,
            ).items():
                mapping.setdefault(k, v)
            # Escape from preview back to admin hub (must win over any collision)
            exit_label = (
                kb._t(ui, "btn_adm_exit_preview") or "🛠 بازگشت به پنل ادمین"
            ).strip()
            mapping[exit_label] = kb.REPLY_ACTION_ADMIN
            admin_label = (kb._t(ui, "btn_admin") or "").strip()
            if admin_label:
                mapping[admin_label] = kb.REPLY_ACTION_ADMIN
        if role == Role.RESELLER.value and is_reseller_bot:
            for k, v in kb.reply_action_map(
                Role.USER.value,
                has_services=has,
                ui=ui,
                as_user=True,
                include_submenus=not free_text,
                is_reseller_bot=True,
                profile=None,
            ).items():
                mapping.setdefault(k, v)
            # Escape from preview back to shop hub (+ custom exit label)
            mapping[(kb._t(ui, "btn_reseller") or "🤝 پنل نماینده").strip()] = kb.REPLY_ACTION_RESELLER
            exit_label = (
                kb._t(ui, "btn_adm_exit_preview") or "🛠 بازگشت به پنل ادمین"
            ).strip()
            mapping[exit_label] = kb.REPLY_ACTION_RESELLER
            preview_label = (kb._t(ui, "btn_adm_preview") or "").strip()
            if preview_label:
                mapping[preview_label] = kb.REPLY_ACTION_RES_PREVIEW
            mapping.setdefault("👁 پیش‌نمایش منوی کاربر", kb.REPLY_ACTION_RES_PREVIEW)
        # Free-text prompts keep only the labels of the visible main keyboard, so a
        # message like «تست» is sent instead of opening a hidden submenu.
        if not free_text:
            await _apply_nav_level_labels(
                mapping, state, ui=ui, role=role, is_reseller_bot=is_reseller_bot
            )
        if kb.is_home_text(text, ui):
            action = kb.REPLY_ACTION_HOME
        elif text in {kb._back_label(ui), kb.BTN_BACK} or (
            kb.normalize_reply_label(text) == kb.normalize_reply_label(kb._back_label(ui))
        ):
            action = kb.REPLY_ACTION_BACK
        else:
            # Exact match, then normalized (strips leftover leading emoji/icons).
            action = kb.resolve_reply_mapping(mapping, text)
        if not action:
            return False
        return {
            "reply_action": action,
            "reply_ui": ui,
            "reply_role": role,
        }

async def _nav_context(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> tuple[dict, str, bool, bool]:
    from app.services.reseller_access import effective_menu_role, is_shop_owner_on_main_bot

    ui = await get_all_settings(session)
    role = await effective_menu_role(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    from app.bot.menu_nav import user_sees_my_services

    has = await user_sees_my_services(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    show_creds = is_shop_owner_on_main_bot(db_user, is_reseller_bot=is_reseller_bot)
    return ui, role, has, show_creds

async def open_shop_list(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.handlers.shop import (
        _record_shop_funnel,
        _shop_category_menu,
        _shop_kind_flags,
        _shop_picker_copy,
        present_shop_kind_picker,
        shop_under_maintenance,
    )

    ui, fixed_on, trial_on, custom_on, wholesale_on, _plans, fixed_plans, _trial = (
        await _shop_kind_flags(session, db_user)
    )
    maint = await shop_under_maintenance(session, ui)
    if maint:
        from app.services.rich_text import outbound_setting_text

        text, send_kw = outbound_setting_text(maint, title="🛠 فروشگاه")
        await message.answer(text, **send_kw)
        return
    await _record_shop_funnel(session, db_user, "shop_open", ui=ui)
    if not any((fixed_on, trial_on, custom_on, wholesale_on)):
        from app.bot.menu_nav import build_main_reply_keyboard
        from app.services.rich_text import outbound_setting_text

        text, send_kw = outbound_setting_text(
            ui.get("shop_empty_text") or feedback.NO_PLANS_FOR_SALE,
            title="🛒 فروشگاه",
        )
        main_kb, _, _ = await build_main_reply_keyboard(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        await message.answer(text, reply_markup=main_kb, **send_kw)
        return
    await state.set_state(None)
    await state.update_data(_shop_custom=custom_on, _shop_wholesale=wholesale_on)
    cats, include_other = await _shop_category_menu(session, fixed_plans)
    body, _cap = _shop_picker_copy(use_categories=bool(cats))
    # Shop bubble carries inline kinds; lasting reply chrome is attached separately
    # (never delete chrome — that clears the keyboard / 4-square menu on iOS).
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_SHOP, push=push)
    await present_shop_kind_picker(
        message,
        ui=ui,
        body=body,
        fixed_on=fixed_on,
        trial_on=trial_on,
        custom_on=custom_on,
        wholesale_on=wholesale_on,
        categories=cats,
        include_uncategorized=include_other,
        mode="send",
    )

async def open_services_list(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.handlers.services import _reseller_capacity_list_label
    from app.bot.menu_nav import build_main_reply_keyboard
    from app.bot.nav_inline import present_inline_only
    ui = await get_all_settings(session)
    services = await list_customer_services(session, db_user.id, current_shop_reseller_id())
    reseller_label = await _reseller_capacity_list_label(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    main_kb, _, _ = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_SERVICES, push=push)
    from app.bot.service_import_ui import EMPTY_LIST_HINT, empty_services_keyboard, import_enabled

    allow_import = import_enabled(ui)
    if not services and not reseller_label:
        from app.services.rich_text import outbound_setting_text

        text, send_kw = outbound_setting_text(
            ui.get("empty_services_text")
            or feedback.NO_SERVICES_YET
        )
        if allow_import:
            await present_inline_only(
                message, text=f"{text}\n\n{EMPTY_LIST_HINT}", inline=empty_services_keyboard(ui), **send_kw
            )
            return
        await message.answer(text, reply_markup=main_kb, **send_kw)
        return
    # Exactly one customer service and no reseller capacity → open card (Wave B).
    # With «افزودن سرویس قبلی» on, the list stays so the add button is reachable.
    if len(services) == 1 and not reseller_label and not allow_import:
        from app.bot.handlers.services import svc_view

        bubble = await message.answer("⏳")
        cb = _SoftCallback(bubble, f"svc:view:{int(services[0].id)}")
        try:
            await svc_view(cb, session, db_user, state)
        except TypeError:
            await svc_view(cb, session, db_user)
        return
    # Only reseller capacity → open that card directly.
    if not services and reseller_label:
        from app.bot.handlers.services import svc_reseller_view

        bubble = await message.answer("⏳")
        cb = _SoftCallback(bubble, "svc:reseller")
        await svc_reseller_view(
            cb,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    from app.bot.list_pages import page_line
    from app.bot.nav_inline import services_list_keyboard, services_page

    body = with_caption(screen_heading(ui, "c.services"), screen_caption(ui, "c.services")) + page_line(
        services_page(services)
    )

    await present_inline_only(
        message,
        text=body,
        inline=services_list_keyboard(
            services, ui, reseller_capacity_label=reseller_label, allow_import=allow_import
        ),
    )
    return

async def open_wallet_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
) -> None:
    from app.bot.nav_inline import present_inline_only, wallet_hub_keyboard
    from app.config import get_settings
    from app.services.formatting import format_toman, kv_line
    from app.services.wallet import wallet_balance_for_context

    ui = await get_all_settings(session)
    bal = await wallet_balance_for_context(session, db_user)
    text = format_message(
        screen_title_html(ui, "c.wallet"),
        "\n".join(
            [
                kv_line(
                    "💵",
                    "موجودی",
                    f"<b>{format_toman(bal, get_settings().currency)}</b>",
                ),
                "",
                screen_caption(ui, "c.wallet"),
            ]
        ),
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_WALLET, push=push)
    await present_inline_only(
        message, text=text, inline=wallet_hub_keyboard(ui)
    )
    return

async def open_wallet_topup(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    from app.services.users import on

    ui = await get_all_settings(session)
    can_topup = any(
        on(ui.get(k))
        for k in (
            "pay_card_enabled",
            "pay_gateway_enabled",
            "pay_psp_enabled",
            "pay_bbc_enabled",
            "pay_crypto_enabled",
        )
    )
    if not can_topup:
        from app.bot.nav_inline import present_inline_only, wallet_hub_keyboard

        await present_inline_only(
            message,
            text="روش شارژ فعالی تنظیم نشده.",
            inline=wallet_hub_keyboard(ui),
        )
        return
    from app.bot.handlers.wallet import WalletStates

    from app.bot.nav_inline import present_inline_only, wallet_topup_presets_keyboard

    # Presets first; custom amount uses cancel_reply after nv:w:amt:custom.
    await state.set_state(WalletStates.topup_amount)
    await present_inline_only(
        message,
        text=format_message(
            "➕ شارژ کیف پول",
            feedback.PICK_AMOUNT,
        ),
        inline=wallet_topup_presets_keyboard(ui),
    )
    return

async def open_wallet_tx(message: Message, session: AsyncSession, db_user: BotUser) -> None:
    from app.bot.handlers.customer_orders import with_orders_button
    from app.bot.nav_inline import present_inline_only, wallet_hub_keyboard
    from app.config import get_settings
    from app.services.formatting import format_toman
    from app.services.wallet import list_activity

    ui = await get_all_settings(session)
    txs = await list_activity(session, db_user.id, limit=15)
    if not txs:
        body = "تراکنشی ثبت نشده."
        await present_inline_only(
            message, text=body, inline=with_orders_button(wallet_hub_keyboard(ui))
        )
        return
    lines = []
    for t in txs:
        sign = "+" if t.amount >= 0 else ""
        lines.append(
            f"{sign}{format_toman(t.amount, get_settings().currency)} — {t.reason}"
        )
    body = format_message("📜 تراکنش‌ها", "\n".join(lines))
    await present_inline_only(
        message, text=body, inline=with_orders_button(wallet_hub_keyboard(ui))
    )

async def open_support_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.nav_inline import present_inline_only, support_hub_keyboard
    from app.services.support_contacts import (
        active_support_contacts,
        parse_support_contacts,
        support_chat_url,
    )
    from app.services.rich_text import outbound_setting_text

    _ = reseller_owner_id
    ui = await get_all_settings(session)
    contacts = active_support_contacts(parse_support_contacts(ui.get("support_contacts")))
    default_support = "تیکت جدید بسازید یا تیکت‌های قبلی را ببینید."
    text, send_kw = outbound_setting_text(
        ui.get("support_text") or default_support,
        title_raw=screen_title_raw(ui, "c.support"),
    )
    contact_rows: list[list[InlineKeyboardButton]] = []
    for c in contacts:
        url = support_chat_url(c.get("telegram") or "")
        title = c.get("title") or "پشتیبان"
        if url:
            contact_rows.append(
                [InlineKeyboardButton(text=f"💬 گفتگو با {title}", url=url)]
            )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_SUPPORT, push=push)
    await present_inline_only(
        message,
        text=text,
        inline=support_hub_keyboard(ui, contact_rows=contact_rows or None),
        **send_kw,
    )
    return

async def open_support_new(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    from app.bot.handlers.support import SupportStates
    from app.bot.nav_input import ask_text

    _ = session
    await ask_text(
        message,
        state,
        prompt="موضوع تیکت را بنویسید:",
        cancel_code="s_subj",
        fsm_state=SupportStates.subject,
    )

async def open_support_list(
    message: Message, session: AsyncSession, db_user: BotUser
) -> None:
    from app.bot.nav_inline import present_inline_only
    from app.services.tickets import list_user_tickets

    ui = await get_all_settings(session)
    tickets = await list_user_tickets(session, db_user.id)
    if not tickets:
        await present_inline_only(
            message,
            text=feedback.NO_TICKETS,
            inline=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        kb.back_button(ui, "nv:s:home")
                    ]
                ]
            ),
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{t.id} — {(t.subject or '')[:28]}",
                callback_data=f"support:view:{t.id}",
            )
        ]
        for t in tickets[:20]
    ]
    rows.append(
        [
            kb.back_button(ui, "nv:s:home")
        ]
    )
    await present_inline_only(
        message,
        text="📋 تیکت‌های شما — یکی را باز کنید:",
        inline=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    return

async def open_referral(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
) -> None:
    """Legacy label — invite lives under باشگاه مشتریان."""
    from app.bot.handlers.loyalty import open_loyalty_referral_message

    await open_loyalty_referral_message(message, session, db_user, state, push=True)

async def open_loyalty_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
) -> None:
    from app.bot.handlers.loyalty import open_loyalty_home_message

    await open_loyalty_home_message(message, session, db_user, state, push=push)

async def open_admin_loyalty_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    push: bool = True,
) -> None:
    from app.bot.handlers.loyalty import open_admin_loyalty_hub as _open

    await _open(
        message,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        push=push,
    )

async def open_reseller_apply(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.handlers.reseller import (
        _resapply_home_keyboard,
        _resapply_picker_copy,
    )
    from app.bot.menu_nav import build_main_reply_keyboard
    from app.services.resellers import list_active_reseller_plans

    ui = await get_all_settings(session)
    order_keys = [p.strip() for p in (ui.get("menu_order") or "").split(",") if p.strip()]
    main_kb, _, _ = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if "reseller_apply" not in order_keys:
        await message.answer("درخواست نمایندگی در منو فعال نیست.", reply_markup=main_kb)
        return
    if db_user.role == Role.RESELLER.value:
        await message.answer("شما هم‌اکنون نماینده هستید.", reply_markup=main_kb)
        return
    if db_user.role == Role.ADMIN.value:
        await message.answer("ادمین نیاز به درخواست ندارد.", reply_markup=main_kb)
        return
    all_plans = await list_active_reseller_plans(session)
    if not all_plans:
        await message.answer(
            format_message(
                "نمایندگی",
                feedback.NO_RESELLER_PLANS,
            ),
            reply_markup=main_kb,
        )
        return
    title, body, _use_cats = await _resapply_picker_copy(session)
    text = format_message(title, body)
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_RESELLER_APPLY, push=push)
    inline = await _resapply_home_keyboard(session, ui)
    from app.bot.nav_inline import present_inline_only

    await present_inline_only(message, text=text, inline=inline)
    return

async def _open_main_bot_reseller_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None,
    *,
    push: bool,
) -> None:
    from app.bot.auth import bot_may_open_pg_hub
    from app.bot.nav_inline import present_nav_panel, reseller_main_bot_hub_keyboard
    from app.services.reseller_access import load_reseller_capacity_actor

    owner_id, profile = await load_reseller_capacity_actor(
        session, db_user, is_reseller_bot=False, reseller_owner_id=None
    )
    if not owner_id or profile is None:
        await open_reseller_creds(
            message, session, db_user, is_reseller_bot=False, reseller_owner_id=None
        )
        return
    try:
        show_pg = await bot_may_open_pg_hub(session, db_user, is_reseller_bot=False)
    except SQLAlchemyError:
        logger.warning("reseller hub pg gate failed", exc_info=True)
        show_pg = False
    ui = await get_all_settings(session)
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_RESELLER, push=push)
    await present_nav_panel(
        message,
        text=format_message(
            screen_title_html(ui, "r.home"), screen_caption(ui, "r.home")
        ),
        inline=reseller_main_bot_hub_keyboard(profile, ui, show_pg=show_pg),
        state=state,
    )


async def open_reseller_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
    push: bool = True,
) -> None:
    from app.bot.handlers.reseller_hub import load_reseller_hub
    from app.bot.nav_inline import present_nav_panel, reseller_manage_hub_keyboard
    from app.services.admin_counters import format_queue_summary

    if not is_reseller_bot:
        await _open_main_bot_reseller_hub(
            message, session, db_user, state, push=push
        )
        return
    hub = await load_reseller_hub(session, db_user, reseller_owner_id=reseller_owner_id)
    if hub is None:
        await message.answer(feedback.RESELLER_ACCESS_NOT_FOUND)
        return
    ui, queue = hub.ui, hub.queue
    body = format_message(screen_title_html(ui, "r.home"), screen_caption(ui, "r.home"))
    queue_line = format_queue_summary(queue)
    if queue_line:
        body = f"{body}\n\n{queue_line}"
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_RESELLER, push=push)
    await present_nav_panel(
        message,
        text=body,
        inline=reseller_manage_hub_keyboard(
            hub.profile,
            ui,
            can_add_representative=hub.can_add_representative,
            pending_payments=queue.payments,
            pending_tickets=queue.tickets,
            pending_cancellations=queue.cancellations,
            pending_deliveries=queue.deliveries,
        ),
        state=state,
    )


async def open_reseller_creds(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.menu_nav import build_main_reply_keyboard
    from app.services.resellers import format_reseller_access_card, get_reseller_profile

    main_kb, _, _ = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if db_user.role != Role.RESELLER.value:
        await message.answer("فقط نمایندگان.", reply_markup=main_kb)
        return
    profile = await get_reseller_profile(session, db_user.id)
    if not profile or not profile.is_active:
        await message.answer("پروفایل نماینده یافت نشد.", reply_markup=main_kb)
        return
    from app.bot.nav_inline import present_nav_panel, reseller_access_card_keyboard

    ui = await get_all_settings(session)
    await present_nav_panel(
        message,
        text=await format_reseller_access_card(session, profile),
        inline=reseller_access_card_keyboard(profile, ui),
        state=state,
    )


async def open_pg_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.auth import (
        OWNER_REQUIRED_MESSAGE,
        bot_may_open_pg_hub,
        bot_migrated_pg_features,
        bot_pg_can_create_user,
    )
    from app.bot.nav_inline import present_nav_panel, pg_hub_keyboard
    from app.services.bot_principal_identity import shop_bot_actor_is_operator

    if is_reseller_bot and not await shop_bot_actor_is_operator(
        session,
        db_user,
        is_reseller_bot=True,
        reseller_owner_id=reseller_owner_id,
    ):
        await _refuse_admin(message)
        return
    if not await bot_may_open_pg_hub(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    ):
        await message.answer(OWNER_REQUIRED_MESSAGE + ".")
        return
    ui = await get_all_settings(session)
    feats: frozenset[str] = frozenset()
    can_create = False
    try:
        feats = await bot_migrated_pg_features(
            session, db_user, is_reseller_bot=is_reseller_bot
        )
        can_create = await bot_pg_can_create_user(
            session, db_user, is_reseller_bot=is_reseller_bot
        )
    except Exception:
        feats = frozenset()
        can_create = False
    body = (
        with_caption(screen_heading(ui, "a.pg"), screen_caption(ui, "a.pg"))
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_PG, push=push)
    is_reseller_actor = is_reseller_bot or db_user.role == Role.RESELLER.value
    back_callback = "nv:res:home" if is_reseller_actor else "nv:adm:product"
    if not feats:
        from app.bot.nav_inline import admin_product_hub_keyboard

        unavailable_keyboard = (
            pg_hub_keyboard(ui, features=feats, back_callback=back_callback)
            if is_reseller_actor
            else admin_product_hub_keyboard(ui, pg_features=feats)
        )
        await present_nav_panel(
            message,
            text="🖥 پاسارگارد در دسترس نیست — به محصول برگردید.",
            inline=unavailable_keyboard,
            state=state,
        )
        return
    await present_nav_panel(
        message,
        text=body,
        inline=pg_hub_keyboard(
            ui,
            features=feats,
            can_create_user=can_create,
            back_callback=back_callback,
        ),
        state=state,
    )
    return

async def dispatch_admin_inline_reply_action(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    action: str,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    reseller_profile_id: int | None = None,
) -> None:
    """Wave E: run selected admin reply-actions from ``nv:adm:ra:*`` hubs.

    Reuses the same helpers as ``reply_main_nav`` (no new money/delivery logic).
    Owner Principal required for every action (parity with ``_OWNER_ONLY_REPLY_ACTIONS``).
    """
    _ = (reseller_owner_id, reseller_profile_id)
    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    if action == kb.REPLY_ACTION_ADM_ST_PANEL or action == "adm_st_panel":
        from app.bot.handlers.admin_settings import _panel_settings_url

        url = await _panel_settings_url(session, for_shop=False)
        await message.answer(
            "🌐 <b>تنظیمات کامل وب‌پنل</b>\n"
            "ظاهر ربات، رنگ دکمه‌ها، گزارش روزانه، لینک‌ها و متن‌های بلند:\n"
            f"<code>{url}</code>"
        )
        return
    if action == kb.REPLY_ACTION_ADM_PLANS_ADD or action == "adm_plans_add":
        data = await state.get_data()
        aud = data.get("_adm_plans_aud") or "users"
        if aud not in {"users", "resellers"}:
            await open_admin_plans_hub(
                message, session, db_user, state, is_reseller_bot=is_reseller_bot
            )
            return
        from app.bot.nav_inline import (
            admin_plans_add_type_hub_keyboard,
            present_nav_panel,
        )

        ui = await get_all_settings(session)
        await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_ADD_TYPE, push=True)
        await present_nav_panel(
            message,
            text=feedback.PICK_PLAN_TYPE,
            inline=admin_plans_add_type_hub_keyboard(str(aud), ui),
            state=state,
        )
        return

    if action in {
        kb.REPLY_ACTION_ADM_PLANS_CATEGORIES,
        "adm_plans_categories",
    }:
        from app.bot.handlers.plan_catalog_manage import open_categories_manage

        await open_categories_manage(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        return
    if action in {kb.REPLY_ACTION_ADM_PLANS_ADDONS, "adm_plans_addons"}:
        from app.bot.handlers.plan_catalog_manage import open_addons_manage

        await open_addons_manage(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        return
    kind_actions = {
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS,
    }
    if action in kind_actions:
        kind_map = {
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED: "fixed",
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM: "custom",
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL: "trial",
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE: "wholesale",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED: "fixed",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG: "payg",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL: "addon_volume",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS: "addon_users",
        }
        kind = kind_map[action]
        data = await state.get_data()
        aud = data.get("_adm_plans_aud") or (
            "resellers"
            if action
            in {
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED,
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG,
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL,
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS,
            }
            else "users"
        )
        from app.bot.handlers.admin_plans import open_add_kind_action

        await open_add_kind_action(message, session, db_user, state, aud, kind)
        return
    await message.answer("این گزینه در حالت اینلاین پشتیبانی نمی‌شود.")

async def present_admin_plans_audience(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None,
    *,
    audience: str,
    is_reseller_bot: bool = False,
    page_index: int = 0,
) -> None:
    """One audience's plans with their add / catalog actions, in one panel.

    Owner-gated (same Owner Principal gate as ``_OWNER_ONLY_REPLY_ACTIONS``).
    """
    from app.bot.handlers.admin_plans import present_plans_overview

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_KIND, push=True)
    await present_plans_overview(message, session, state, audience, page_index)


async def open_admin_users_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    from app.bot.nav_inline import admin_users_hub_keyboard, present_nav_panel

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    counts = await admin_customer_counts(session)
    total, blocked, orders = counts["users"], counts["blocked"], counts["orders"]
    body = (
        f"{screen_heading(ui, 'a.users')}\n\n"
        f"کل: {total}\n"
        f"مسدود: {blocked}\n"
        f"سفارش‌ها: {orders}\n\n"
        f"{screen_caption(ui, 'a.users')}"
    ).rstrip()
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_USERS, push=push)
    await present_nav_panel(
        message, text=body, inline=admin_users_hub_keyboard(ui), state=state
    )
    return

async def open_admin_resellers_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    from app.bot.nav_inline import admin_resellers_hub_keyboard, present_nav_panel
    from app.bot.auth import platform_can_manage_representatives

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    if not await platform_can_manage_representatives():
        await message.answer(feedback.REPRESENTATIVES_DISABLED)
        return
    ui = await get_all_settings(session)
    body = (
        with_caption(screen_heading(ui, "a.resellers"), screen_caption(ui, "a.resellers"))
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_RESELLERS, push=push)
    await present_nav_panel(
        message, text=body, inline=admin_resellers_hub_keyboard(ui), state=state
    )
    return

async def open_admin_settings_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.nav_inline import admin_settings_hub_keyboard, present_nav_panel

    _ = (reseller_profile_id, reseller_owner_id)
    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    body = (
        with_caption(screen_heading(ui, "a.settings"), screen_caption(ui, "a.settings"))
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_SETTINGS, push=push)
    await present_nav_panel(
        message, text=body, inline=admin_settings_hub_keyboard(ui), state=state
    )
    return

async def open_admin_backup_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> None:
    import asyncio

    from app.bot.handlers import admin_backup as backup_h
    from app.bot.nav_inline import present_nav_panel
    from app.services.backup import list_backups

    _ = (reseller_profile_id, reseller_owner_id)
    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    backups = await asyncio.to_thread(list_backups)
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_BACKUP, push=push)
    await present_nav_panel(
        message,
        text=backup_h._hub_text(backups, ui),
        inline=backup_h.backup_hub_markup(backups, ui),
        state=state,
    )
    return

async def open_admin_broadcast_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.nav_inline import admin_broadcast_hub_keyboard, present_nav_panel

    _ = (reseller_profile_id, reseller_owner_id)
    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    body = (
        with_caption(screen_heading(ui, "a.broadcast"), screen_caption(ui, "a.broadcast"))
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_BROADCAST, push=push)
    await present_nav_panel(
        message, text=body, inline=admin_broadcast_hub_keyboard(ui), state=state
    )
    return

async def open_admin_plans_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.nav_inline import admin_plans_audience_hub_keyboard, present_nav_panel

    _ = (reseller_profile_id, reseller_owner_id)
    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    await state.set_state(None)
    await state.update_data(_adm_plans_aud=None, _adm_plans_kind=None)
    body = (
        with_caption(screen_heading(ui, "a.plans"), screen_caption(ui, "a.plans"))
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_AUDIENCE, push=push)
    await present_nav_panel(
        message, text=body, inline=admin_plans_audience_hub_keyboard(ui), state=state
    )
    return

async def open_reseller_settings_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
    push: bool = True,
) -> None:
    from app.services.reseller_access import load_reseller_actor
    from app.services.resellers import has_bot_perm

    if not is_reseller_bot:
        await open_reseller_creds(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile:
        await message.answer(feedback.RESELLER_ACCESS_NOT_FOUND)
        return
    if not has_bot_perm(profile, "shop_settings"):
        await message.answer(feedback.NO_SHOP_SETTINGS_ACCESS)
        return
    bot_line = f"@{profile.bot_username}" if profile.bot_username else feedback.BOT_TOKEN_NOT_SET
    from app.bot.nav_inline import present_nav_panel, reseller_settings_hub_keyboard

    ui = await get_all_settings(session)
    body = with_caption(
        screen_heading(ui, "r.settings"), f"ربات: <code>{bot_line}</code>", screen_caption(ui, "r.settings")
    )
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_RESELLER_SETTINGS, push=push)
    await present_nav_panel(
        message, text=body, inline=reseller_settings_hub_keyboard(ui), state=state
    )
    return

async def open_reseller_plans_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
    push: bool = True,
    page_index: int = 0,
) -> None:
    from app.bot.handlers.reseller_plans import _list_plans
    from app.bot.list_pages import page_line, page_of
    from app.services.reseller_access import load_reseller_actor
    from app.services.resellers import has_bot_perm

    if not is_reseller_bot:
        await open_reseller_creds(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile:
        await message.answer(feedback.RESELLER_ACCESS_NOT_FOUND)
        return
    if not has_bot_perm(profile, "plans"):
        await message.answer("دسترسی پلن ندارید.")
        return
    from app.bot.nav_inline import (
        present_nav_panel,
        reseller_plans_hub_keyboard,
        with_inline_back,
    )
    from app.services.users import get_all_settings
    from aiogram.types import InlineKeyboardMarkup

    ui = await get_all_settings(session, reseller_id=owner_id)
    plans = await _list_plans(session, owner_id)
    page = page_of(plans, page_index, kb.RESELLER_PLANS_PAGE_SIZE)
    list_body = "هنوز پلنی نساخته‌اید." if not plans else f"تعداد: {len(plans)}" + page_line(page)
    hub_text = with_caption(screen_heading(ui, "r.plans"), screen_caption(ui, "r.plans"))
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_RESELLER_PLANS, push=push)
    hub = reseller_plans_hub_keyboard(ui)
    hub_rows = [list(r) for r in hub.inline_keyboard[:-1]]
    list_kb = with_inline_back(
        kb.reseller_plans_list_keyboard(plans, ui, page_index=page.index), ui, back_callback_for("res_plans")
    )
    combined = InlineKeyboardMarkup(
        inline_keyboard=hub_rows + [list(r) for r in list_kb.inline_keyboard]
    )
    await present_nav_panel(
        message,
        text=f"{hub_text}\n\n📦 <b>لیست</b>\n{list_body}",
        inline=combined,
        state=state,
    )
    return

async def open_admin_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    from app.bot.nav_inline import admin_groups_hub_keyboard, present_nav_panel
    from app.services.admin_counters import format_queue_summary, pending_counts
    from app.version import __version__ as local_version

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    # Platform-only queue — never include shop-tenant rows.
    from app.services.admin_counters import PendingCounts

    queue = PendingCounts()
    try:
        queue = await pending_counts(session, shop_id=None)
    except Exception:
        logger.warning("admin home pending_counts failed", exc_info=True)
    body = with_caption(f"{screen_heading(ui, 'a.home')}\n<code>v{local_version}</code>\n", screen_caption(ui, "a.home"))
    queue_line = format_queue_summary(queue)
    if queue_line:
        body = f"{body}\n\n{queue_line}"
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN, push=push)
    await present_nav_panel(
        message,
        text=body,
        inline=admin_groups_hub_keyboard(
            ui,
            pending_payments=queue.payments,
            pending_tickets=queue.tickets,
            pending_cancellations=queue.cancellations,
            pending_deliveries=queue.deliveries,
        ),
        state=state,
    )
    return

async def open_admin_ops_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    from app.bot.nav_inline import admin_ops_hub_keyboard, present_nav_panel
    from app.services.admin_counters import (
        PendingCounts,
        format_queue_summary,
        pending_counts,
    )

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    try:
        queue = await pending_counts(session, shop_id=None)
    except Exception:
        logger.warning("admin ops pending_counts failed", exc_info=True)
        queue = PendingCounts()
    body = with_caption(screen_heading(ui, "a.ops"), screen_caption(ui, "a.ops"))
    queue_line = format_queue_summary(queue)
    if queue_line:
        # Cancel count is summary-only (no bot cancel inbox yet).
        body = f"{body}\n\n{queue_line}"
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_OPS, push=push)
    await present_nav_panel(
        message,
        text=body,
        inline=admin_ops_hub_keyboard(
            ui,
            pending_payments=queue.payments,
            pending_tickets=queue.tickets,
            pending_deliveries=queue.deliveries,
        ),
        state=state,
    )
    return

async def open_admin_people_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    from app.bot.nav_inline import admin_people_hub_keyboard, present_nav_panel

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    _, can_reps = await nav._platform_admin_menu_flags(
        session, db_user, is_reseller_bot=is_reseller_bot
    )
    body = with_caption(screen_heading(ui, "a.people"), screen_caption(ui, "a.people"))
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_PEOPLE, push=push)
    await present_nav_panel(
        message,
        text=body,
        inline=admin_people_hub_keyboard(
            ui, can_manage_representatives=can_reps
        ),
        state=state,
    )
    return

async def open_admin_product_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    from app.bot.nav_inline import admin_product_hub_keyboard, present_nav_panel

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    pg_feats, _ = await nav._platform_admin_menu_flags(
        session, db_user, is_reseller_bot=is_reseller_bot
    )
    body = with_caption(screen_heading(ui, "a.product"), screen_caption(ui, "a.product"))
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_PRODUCT, push=push)
    await present_nav_panel(
        message,
        text=body,
        inline=admin_product_hub_keyboard(ui, pg_features=pg_feats),
        state=state,
    )
    return

async def open_admin_system_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    from app.bot.nav_inline import admin_system_hub_keyboard, present_nav_panel

    if not await _deny_unless_owner(
        message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    ui = await get_all_settings(session)
    body = with_caption(screen_heading(ui, "a.system"), screen_caption(ui, "a.system"))
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_ADMIN_SYSTEM, push=push)
    await present_nav_panel(
        message,
        text=body,
        inline=admin_system_hub_keyboard(ui),
        state=state,
    )
    return

async def open_user_preview(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.services.users import get_all_settings

    ui = await get_all_settings(session)
    exit_label = (kb._t(ui, "btn_adm_exit_preview") or "🛠 بازگشت به پنل ادمین").strip()
    await nav.show_nav_keyboard(
        message,
        session,
        db_user,
        nav.NAV_USER_PREVIEW,
        text=format_message(
            "👁 پیش‌نمایش منوی کاربر",
            f"کیبورد پایین به حالت کاربر تغییر کرد. برای خروج «{exit_label}» را بزنید.",
        ),
        state=state,
        push=True,
        as_user=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

async def _refuse_admin(message: Message) -> None:
    await message.answer("دسترسی ادمین فقط روی ربات اصلی پلتفرم مجاز است.")

async def _deny_unless_owner(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
) -> bool:
    """Phase 4G — reply-nav Owner gate (same Principal as admin callbacks)."""
    from app.bot.auth import OWNER_REQUIRED_MESSAGE, is_bot_owner_principal

    if is_reseller_bot:
        await _refuse_admin(message)
        return False
    if not await is_bot_owner_principal(
        session, db_user, is_reseller_bot=is_reseller_bot
    ):
        await message.answer(OWNER_REQUIRED_MESSAGE + ".")
        return False
    return True

# Platform-plan reply keys used to skip the hub opener. Must still be Owner-only
# even if L2/L1 forges the keyboard action (Phase 5D).
_OWNER_ONLY_REPLY_ACTIONS = frozenset(
    {
        kb.REPLY_ACTION_ADM_PLANS_AUD_USERS,
        kb.REPLY_ACTION_ADM_PLANS_AUD_RESELLERS,
        kb.REPLY_ACTION_ADM_PLANS_ADD,
        kb.REPLY_ACTION_ADM_PLANS_CATEGORIES,
        kb.REPLY_ACTION_ADM_PLANS_ADDONS,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS,
    }
)

async def _soft_admin(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    data: str,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.auth import is_migrated_pg_soft_callback
    from app.bot.handlers import admin as admin_h
    from app.bot.handlers import admin_backup as backup_h
    from app.bot.handlers import admin_plans as plans_h
    from app.bot.handlers import admin_settings as settings_h

    shop_kw = dict(
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if is_reseller_bot:
        from app.services.bot_principal_identity import shop_bot_actor_is_operator

        if not is_migrated_pg_soft_callback(data) or not await shop_bot_actor_is_operator(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        ):
            await _refuse_admin(message)
            return
    if not is_migrated_pg_soft_callback(data):
        if not await _deny_unless_owner(
            message, session, db_user, is_reseller_bot=is_reseller_bot
        ):
            return
        if data.startswith("adm:resellers") or data.startswith("adm:resapp"):
            from app.bot.auth import platform_can_manage_representatives

            if not await platform_can_manage_representatives():
                await message.answer(feedback.REPRESENTATIVES_DISABLED)
                return
    bubble = await message.answer("⏳")
    cb = _SoftCallback(bubble, data)
    try:
        if data == "adm:orders":
            await admin_h.adm_orders(cb, session, db_user)
        elif data.startswith("adm:reports"):
            await admin_h.adm_reports(cb, session, db_user)
        elif data == "adm:payments":
            await admin_h.adm_payments(cb, session, db_user)
        elif data == "adm:tickets":
            await admin_h.adm_tickets(cb, session, db_user)
        elif data == "adm:plans":
            await plans_h.plans_hub(cb, session, db_user, state)
        elif data.startswith("adm:plans:aud:"):
            await plans_h.plans_aud_inline_back(cb, session, db_user, state)
        elif data.startswith("adm:plans:kind:"):
            await plans_h.plans_kind_cb(cb, session, db_user, state)
        elif data == "adm:plan:add":
            await admin_h.adm_plan_add(cb, state, db_user, session=session)
        elif data == "adm:st:sub:service:custom":
            await settings_h.settings_sub(cb, session, db_user)
        elif data == "adm:st:sub:service:trial":
            await settings_h.settings_sub(cb, session, db_user)
        elif data == "adm:pg":
            from app.bot.handlers import admin_pg_users as pg_users_h

            await pg_users_h.adm_pg(cb, db_user, session=session, **shop_kw)
        elif data == "adm:pg:stats":
            await admin_h.pg_stats(cb, db_user, session=session)
        elif data == "adm:pg:nodes":
            from app.bot.handlers import admin_pg_nodes as pg_nodes_h

            await pg_nodes_h.pg_nodes(cb, db_user, session=session, **shop_kw)
        elif data == "adm:pg:group":
            from app.bot.handlers import admin_pg_users as pg_users_h

            await pg_users_h.adm_pg_group_hint(cb, db_user, session=session, **shop_kw)
        elif data == "adm:pg:template":
            from app.bot.handlers import admin_pg_users as pg_users_h

            await pg_users_h.adm_pg_template_hint(
                cb, db_user, session=session, **shop_kw
            )
        elif data == "adm:pg:users":
            from app.bot.handlers import admin_pg_users as pg_users_h

            await pg_users_h.pg_users_list(
                cb, state, db_user, session=session, **shop_kw
            )
        elif data == "adm:pg:search":
            from app.bot.handlers import admin_pg_users as pg_users_h

            await pg_users_h.pg_search_start(
                cb, state, db_user, session=session, **shop_kw
            )
        elif data == "adm:pg:create":
            from app.bot.handlers import admin_pg_users as pg_users_h

            await pg_users_h.pg_create_menu(
                cb, state, db_user, session=session, **shop_kw
            )
        elif data == "adm:users:list:0":
            await admin_h.adm_users_list(cb, session, db_user)
        elif data == "adm:users:search":
            await admin_h.adm_users_search_start(cb, state, db_user, session=session)
        elif data == "adm:users:webhint":
            await admin_h.adm_users_webhint(cb, db_user, session=session)
        elif data == "adm:resellers:list:0":
            await admin_h.adm_resellers_list(cb, session, db_user)
        elif data == "adm:resellers:search":
            await admin_h.adm_resellers_search_start(cb, state, db_user, session=session)
        elif data == "adm:resapp:list":
            await admin_h.adm_resapp_list(cb, session, db_user)
        elif data == "adm:resellers:add":
            await admin_h.adm_resellers_add(cb, state, db_user, session=session)
        elif data.startswith("adm:st:sec:"):
            await settings_h.settings_section(cb, session, db_user)
        elif data == "adm:dash":
            await admin_h.adm_dash(cb, session, db_user)
        elif data == "adm:resellers":
            await admin_h.adm_resellers(cb, db_user, state, session=session)
        elif data == "adm:backup":
            await backup_h.backup_hub(cb, db_user, state, session=session)
        elif data == "adm:backup:create":
            await backup_h.backup_create(cb, db_user, session=session)
        elif data == "adm:backup:create:noenv":
            await backup_h.backup_create(cb, db_user, session=session)
        elif data == "adm:backup:create:env":
            await backup_h.backup_create_env_ask(cb, db_user, session=session)
        elif data == "adm:backup:create:env:yes":
            await backup_h.backup_create(cb, db_user, session=session)
        elif data == "adm:backup:upload":
            await backup_h.backup_upload_ask(cb, db_user, state, session=session)
        elif data == "adm:broadcast":
            await bubble.edit_text("مخاطب پیام گروهی را انتخاب کنید.", reply_markup=_admin_back("nv:adm:broadcast"))
        elif data.startswith("adm:broadcast:aud:"):
            await admin_h.adm_broadcast_audience(cb, db_user, state, session=session)
        elif data in {"adm:settings", "adm:st:hub"}:
            if state is not None:
                await settings_h.settings_hub(cb, session, db_user, state)
            else:
                await bubble.edit_text("⚙️ تنظیمات", reply_markup=_admin_back("nv:adm:settings"))
        else:
            await bubble.edit_text("این بخش در دسترس نیست.", reply_markup=_admin_back("nv:adm:home"))
    except Exception as e:
        try:
            await bubble.edit_text(f"خطا: {user_safe_error(e)}", reply_markup=_admin_back("nv:adm:home"))
        except Exception:
            logger.debug("_soft_admin: ignored failure", exc_info=True)

async def handle_back(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    reseller_profile_id: int | None = None,
) -> None:
    """One-level back: within-section step first, then reply-keyboard stack."""
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data(data)

    # 1) Within shop: plans/custom/wholesale → shop hub (not straight to home).
    current = await nav.get_nav_level(state)
    if current == nav.NAV_SHOP:
        step = str(data.get(nav.SHOP_STEP) or "hub")
        if step and step != "hub":
            await nav.set_shop_step(state, "hub")
            await open_shop_list(
                message,
                session,
                db_user,
                state,
                push=False,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
    # 2) Within loyalty club: sub-screen → club hub.
    if current == nav.NAV_LOYALTY:
        step = str(data.get(nav.LOY_STEP) or "hub")
        if step and step != "hub":
            await nav.set_loy_step(state, "hub")
            await open_loyalty_home(message, session, db_user, state, push=False)
            return

    level = await nav.pop_nav_level(state)
    if level == nav.NAV_SERVICES:
        await open_services_list(
            message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if level == nav.NAV_WALLET:
        await open_wallet_home(message, session, db_user, state, push=False)
        return
    if level == nav.NAV_SHOP:
        await open_shop_list(
            message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if level == nav.NAV_RESELLER_APPLY:
        await open_reseller_apply(
            message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if level == nav.NAV_SUPPORT:
        await open_support_home(
            message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if level == nav.NAV_LOYALTY:
        await open_loyalty_home(message, session, db_user, state, push=False)
        return
    if level == nav.NAV_ADMIN_LOYALTY:
        await open_admin_loyalty_hub(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            push=False,
        )
        return
    if level == nav.NAV_ADMIN:
        await open_admin_home(message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot)
        return
    if level == nav.NAV_ADMIN_OPS:
        await open_admin_ops_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_PEOPLE:
        await open_admin_people_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_PRODUCT:
        await open_admin_product_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_MARKETING:
        from app.bot.handlers.nav_marketing import open_admin_marketing_hub

        await open_admin_marketing_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level in {nav.NAV_ADMIN_GIFTS, nav.NAV_ADMIN_CAMPAIGNS}:
        from app.bot.handlers.marketing_tools import open_admin_marketing_panel

        await open_admin_marketing_panel(
            level, message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_SYSTEM:
        await open_admin_system_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_PG:
        await open_pg_home(
            message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if level == nav.NAV_ADMIN_USERS:
        await open_admin_users_hub(message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot)
        return
    if level == nav.NAV_ADMIN_RESELLERS:
        await open_admin_resellers_hub(message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot)
        return
    if level == nav.NAV_ADMIN_SETTINGS:
        await open_admin_settings_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_BACKUP:
        await open_admin_backup_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_BROADCAST:
        await open_admin_broadcast_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level in {nav.NAV_ADMIN_PLANS_ADD_TYPE, nav.NAV_ADMIN_PLANS_KIND}:
        if not await _deny_unless_owner(
            message, session, db_user, is_reseller_bot=is_reseller_bot
        ):
            return
    if level == nav.NAV_ADMIN_PLANS_ADD_TYPE:
        aud = (await state.get_data()).get("_adm_plans_aud") or "users"
        await state.update_data(_adm_plans_kind=None)
        await present_admin_plans_audience(
            message,
            session,
            db_user,
            state,
            audience=str(aud),
            is_reseller_bot=is_reseller_bot,
        )
        return
    if level == nav.NAV_ADMIN_PLANS_KIND:
        await state.update_data(_adm_plans_kind=None)
        await open_admin_plans_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_PLANS_AUDIENCE:
        await open_admin_product_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_ADMIN_PLANS:
        await open_admin_plans_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
        return
    if level == nav.NAV_RESELLER:
        await open_reseller_home(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            push=False,
        )
        return
    if level == nav.NAV_RESELLER_SETTINGS:
        await open_reseller_settings_hub(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            push=False,
        )
        return
    if level == nav.NAV_RESELLER_PLANS:
        await open_reseller_plans_hub(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            push=False,
        )
        return
    if level == nav.NAV_SERVICE:
        # Restored onto service detail card (inline).
        data2 = await state.get_data()
        svc_id = data2.get(nav.SERVICE_ID)
        if svc_id:
            from app.bot.handlers.services import svc_view

            bubble = await message.answer("⏳")
            cb = _SoftCallback(bubble, f"svc:view:{int(svc_id)}")
            try:
                await svc_view(cb, session, db_user, state)
            except TypeError:
                await svc_view(cb, session, db_user)
            return
        await open_services_list(
            message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if level == nav.NAV_USER_PREVIEW or await nav.is_user_preview_active(state):
        # Back / Home from preview exits to the staff panel (same as exit button).
        from app.bot.nav_chrome import heal_main_reply
        from app.bot.nav_inline import clear_nav_panel

        await clear_nav_panel(state)
        await nav.set_user_preview_active(state, False)
        await heal_main_reply(
            message,
            session,
            db_user,
            text=(
                "به پنل نماینده برگشتید."
                if is_reseller_bot
                else "به پنل ادمین برگشتید."
            ),
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            as_user=False,
        )
        if is_reseller_bot:
            await open_reseller_home(
                message,
                session,
                db_user,
                state,
                is_reseller_bot=True,
                reseller_owner_id=reseller_owner_id,
                push=False,
            )
        else:
            await open_admin_home(
                message,
                session,
                db_user,
                state,
                push=False,
                is_reseller_bot=False,
            )
        return
    if level == nav.NAV_PAY:
        data2 = await state.get_data()
        oid = data2.get(nav.PAY_ORDER_ID)
        if oid:
            from app.bot.handlers.shop import navigate_order_pay_back
            from app.db.models import Order

            order = await session.get(Order, int(oid))
            if order is not None and int(order.user_id) == int(db_user.id):
                await navigate_order_pay_back(
                    message,
                    session,
                    db_user,
                    order,
                    state,
                    is_reseller_bot=is_reseller_bot,
                    reseller_owner_id=reseller_owner_id,
                )
                return
        await open_wallet_home(message, session, db_user, state, push=False)
        return
    if level == nav.NAV_TOPUP_PAY:
        await open_wallet_home(message, session, db_user, state, push=False)
        return
    from app.bot.handlers.start import render_home

    await nav.clear_nav(state)
    await render_home(
        message,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

async def _handle_pay_action(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    action: str,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.handlers import shop as shop_h

    data = await state.get_data()
    order_id = data.get(nav.PAY_ORDER_ID)
    if not order_id:
        await message.answer("سفارش یافت نشد. دوباره از فروشگاه انتخاب کنید.")
        return
    order = await session.get(Order, int(order_id))
    if not order or int(order.user_id) != int(db_user.id):
        await restore_main_reply(
            message,
            session,
            db_user,
            text="دسترسی به این سفارش مجاز نیست.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    cb_map = {
        kb.REPLY_ACTION_PAY_WALLET: f"pay:wallet:{order.id}",
        kb.REPLY_ACTION_PAY_CARD: f"pay:card:{order.id}",
        kb.REPLY_ACTION_PAY_GATEWAY: f"pay:gateway:{order.id}",
        kb.REPLY_ACTION_PAY_PSP: f"pay:psp:{order.id}",
        kb.REPLY_ACTION_PAY_BBC: f"pay:bbc:{order.id}",
        kb.REPLY_ACTION_PAY_CRYPTO: f"pay:crypto:{order.id}",
        kb.REPLY_ACTION_PAY_STARS: f"pay:stars:{order.id}",
        kb.REPLY_ACTION_PAY_DISCOUNT: f"pay:discount:{order.id}",
    }
    cb_data = cb_map.get(action)
    if not cb_data:
        return
    bubble = await message.answer("⏳")
    cb = _SoftCallback(bubble, cb_data)
    try:
        if action == kb.REPLY_ACTION_PAY_WALLET:
            await shop_h.pay_wallet_cb(cb, session, db_user, state=state)
        elif action == kb.REPLY_ACTION_PAY_CARD:
            await shop_h.pay_card_cb(cb, session, db_user, state=state)
        elif action == kb.REPLY_ACTION_PAY_GATEWAY:
            await shop_h.pay_gateway_cb(cb, session, db_user, state=state)
        elif action == kb.REPLY_ACTION_PAY_PSP:
            await shop_h.pay_psp_cb(cb, session, db_user, state=state)
        elif action == kb.REPLY_ACTION_PAY_BBC:
            await shop_h.pay_bbc_cb(cb, session, db_user, state=state)
        elif action == kb.REPLY_ACTION_PAY_CRYPTO:
            await shop_h.pay_crypto_cb(cb, session, db_user, state=state)
        elif action == kb.REPLY_ACTION_PAY_STARS:
            await shop_h.pay_stars_cb(cb, session, db_user, state=state)
        elif action == kb.REPLY_ACTION_PAY_DISCOUNT:
            await shop_h.ask_discount(cb, state, session, db_user)
    except Exception as e:
        try:
            await bubble.edit_text(f"خطا: {user_safe_error(e)}")
        except Exception:
            await message.answer(f"خطا: {user_safe_error(e)}")

async def _handle_topup_action(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    action: str,
) -> None:
    from app.bot.handlers import wallet as wallet_h

    key = {
        kb.REPLY_ACTION_TOPUP_CARD: "wtop:card",
        kb.REPLY_ACTION_TOPUP_GATEWAY: "wtop:gateway",
        kb.REPLY_ACTION_TOPUP_PSP: "wtop:psp",
        kb.REPLY_ACTION_TOPUP_BBC: "wtop:bbc",
        kb.REPLY_ACTION_TOPUP_CRYPTO: "wtop:crypto",
    }.get(action)
    if not key:
        return
    bubble = await message.answer("⏳")
    cb = _SoftCallback(bubble, key)
    try:
        await wallet_h.wtop_choose_method(cb, session, state, db_user)
    except Exception as e:
        try:
            await bubble.edit_text(f"خطا: {user_safe_error(e)}")
        except Exception:
            await message.answer(f"خطا: {user_safe_error(e)}")

# Capacity ops are allowed on the platform bot for the shop owner (renew/extras).
_RESELLER_CAPACITY_ACTIONS = frozenset(
    {"res_renew", "res_buy_gb", "res_buy_users", "res_addon_packs"}
)

async def _soft_reseller(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    action: str,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> None:
    from app.bot.handlers import reseller as res_h
    from app.bot.handlers import reseller_plans as res_plans_h
    from app.bot.handlers import reseller_settings as res_st_h
    from app.services.reseller_access import (
        load_reseller_actor,
        load_reseller_capacity_actor,
    )

    if not is_reseller_bot:
        if action not in _RESELLER_CAPACITY_ACTIONS:
            await open_reseller_creds(
                message,
                session,
                db_user,
                state=state,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
        owner_id, profile = await load_reseller_capacity_actor(
            session,
            db_user,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        if not owner_id or not profile:
            await message.answer(feedback.RESELLER_ACCESS_NOT_FOUND)
            return
        # Fall through to capacity mapping below (no shop-panel gates).
    else:
        owner_id, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        if not owner_id or not profile:
            await message.answer(feedback.RESELLER_ACCESS_NOT_FOUND)
            return

    if action == kb.REPLY_ACTION_RES_ADD_REP:
        from app.bot.handlers.reseller_reps import start_add_representative

        await start_add_representative(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return

    if action == "res_settings":
        await open_reseller_settings_hub(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if action == "res_loyalty":
        await open_admin_loyalty_hub(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if action == "res_plans":
        await open_reseller_plans_hub(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if action == "res_plan_add":
        bubble = await _live_bubble(message)
        cb = _SoftCallback(bubble, "res:plan:add", back_callback=back_callback_for("res_plans"))
        try:
            await res_plans_h.res_plan_add(
                cb,
                state,
                session,
                db_user,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
        except Exception as e:
            try:
                await bubble.edit_text(
                    f"خطا: {user_safe_error(e)}", reply_markup=_hub_back_markup()
                )
            except Exception:
                logger.debug("_soft_reseller: ignored failure", exc_info=True)
        return
    if action == kb.REPLY_ACTION_RES_PLAN_CATEGORIES:
        from app.bot.handlers.plan_catalog_manage import open_categories_manage

        await open_categories_manage(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if action == kb.REPLY_ACTION_RES_PLAN_ADDONS:
        from app.bot.handlers.plan_catalog_manage import open_addons_manage

        await open_addons_manage(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if action.startswith("res_st_"):
        sec = action.replace("res_st_", "", 1)
        if sec == "panel":
            from app.services.resellers import get_reseller_panel_base_url, has_bot_perm

            if not has_bot_perm(profile, "shop_settings"):
                await message.answer(feedback.NO_SHOP_SETTINGS_ACCESS)
                return
            base = (await get_reseller_panel_base_url(session) or "").rstrip("/")
            url = f"{base}/shop-settings" if base else "/shop-settings"
            await message.answer(
                "🌐 <b>تنظیمات کامل فروشگاه در وب‌پنل</b>\n"
                f"<code>{url}</code>"
            )
            return
        bubble = await _live_bubble(message)
        cb = _SoftCallback(bubble, f"res:st:sec:{sec}", back_callback=back_callback_for("res_settings"))
        try:
            await res_st_h.settings_section(
                cb,
                session,
                db_user,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
        except Exception as e:
            try:
                await bubble.edit_text(
                    f"خطا: {user_safe_error(e)}", reply_markup=_hub_back_markup()
                )
            except Exception:
                logger.debug("_soft_reseller: ignored failure", exc_info=True)
        return

    mapping = {
        "res_dash": ("res:dash", "res_dash"),
        "res_users": ("res:users:0", "res_users"),
        "res_billing": ("res:billing", "res_billing"),
        "res_reports": ("res:reports:week", "res_reports"),
        "res_stats": ("res:stats", "res_stats"),
        "res_orders": ("res:orders", "res_orders"),
        "res_payments": ("res:payments", "res_payments"),
        "res_tickets": ("res:tickets", "res_tickets"),
        "res_renew": ("res:renew", "res_renew"),
        "res_buy_gb": ("res:buy_gb", "res_buy_gb"),
        "res_buy_users": ("res:buy_users", "res_buy_users"),
        "res_addon_packs": ("res:addons", "res_addons"),
    }
    pair = mapping.get(action)
    if not pair:
        return
    data, fn_name = pair
    # res_users handler name
    if fn_name == "res_users":
        fn_name = "res_users_list"
    bubble = await _live_bubble(message)
    cb = _SoftCallback(bubble, data, back_callback=back_callback_for(action))
    fn = getattr(res_h, fn_name, None)
    if fn is None:
        await bubble.edit_text(
            "این بخش در دسترس نیست.", reply_markup=_hub_back_markup()
        )
        return
    try:
        await fn(
            cb,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    except TypeError:
        await fn(cb, session, db_user)

@router.message(F.text, ReplyMenuTextFilter())
async def reply_main_nav(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    reply_action: str,
    reply_ui: dict,
    reply_role: str,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    reseller_profile_id: int | None = None,
):
    """Handle taps on the reply keyboard."""
    from app.bot.handlers.start import render_home

    action = reply_action
    ui = reply_ui
    role = reply_role

    preserve_state = action in {
        kb.REPLY_ACTION_BACK,
        kb.REPLY_ACTION_PAY_WALLET,
        kb.REPLY_ACTION_PAY_CARD,
        kb.REPLY_ACTION_PAY_GATEWAY,
        kb.REPLY_ACTION_PAY_PSP,
        kb.REPLY_ACTION_PAY_BBC,
        kb.REPLY_ACTION_PAY_CRYPTO,
        kb.REPLY_ACTION_PAY_STARS,
        kb.REPLY_ACTION_PAY_DISCOUNT,
        kb.REPLY_ACTION_TOPUP_CARD,
        kb.REPLY_ACTION_TOPUP_GATEWAY,
        kb.REPLY_ACTION_TOPUP_PSP,
        kb.REPLY_ACTION_TOPUP_BBC,
        kb.REPLY_ACTION_TOPUP_CRYPTO,
        kb.REPLY_ACTION_WALLET_TOPUP,
        kb.REPLY_ACTION_SUPPORT_NEW,
        kb.REPLY_ACTION_SHOP_CUSTOM,
        kb.REPLY_ACTION_SHOP_WHOLESALE,
        "bc_aud_all",
        "bc_aud_users",
        "bc_aud_resellers",
        "bc_aud_admins",
        kb.REPLY_ACTION_ADM_PLANS_AUD_USERS,
        kb.REPLY_ACTION_ADM_PLANS_AUD_RESELLERS,
        kb.REPLY_ACTION_ADM_PLANS_ADD,
        kb.REPLY_ACTION_ADM_PLANS_CATEGORIES,
        kb.REPLY_ACTION_ADM_PLANS_ADDONS,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG,
        "adm_plan_add",
        "res_plan_add",
        kb.REPLY_ACTION_RES_PLAN_CATEGORIES,
        kb.REPLY_ACTION_RES_PLAN_ADDONS,
        "backup_upload",
        # Keep nav stack when opening reseller sub-hubs / sections
        "res_settings",
        "res_plans",
        "res_dash",
        "res_users",
        "res_billing",
        "res_reports",
        "res_stats",
        "res_orders",
        "res_payments",
        "res_tickets",
        "res_loyalty",
        "res_renew",
        "res_buy_gb",
        "res_buy_users",
        "res_addon_packs",
        "res_preview",
        kb.REPLY_ACTION_RES_ADD_REP,
        "res_st_shop",
        "res_st_menu",
        "res_st_pay",
        "res_st_support",
        "res_st_access",
        "res_st_limits",
        "res_st_labels",
        "res_st_buy",
        "res_st_guides",
        "res_st_bot",
        "res_st_notify",
        kb.REPLY_ACTION_RES_ST_PANEL,
        kb.REPLY_ACTION_ADM_ST_PANEL,
        # Keep System → Settings stack when opening a settings section
        "adm_st_shop",
        "adm_st_menu",
        "adm_st_pay",
        "adm_st_support",
        "adm_st_access",
        "adm_st_limits",
        "adm_st_labels",
        "adm_st_buy",
        "adm_st_guides",
        "adm_st_notify",
        kb.REPLY_ACTION_REV_OK,
        kb.REPLY_ACTION_REV_NO,
        kb.REPLY_ACTION_SVC_LINK,
        kb.REPLY_ACTION_SVC_GUIDE,
        kb.REPLY_ACTION_SVC_CANCEL,
        kb.REPLY_ACTION_SVC_RENEW,
        kb.REPLY_ACTION_SVC_ADDON,
        kb.REPLY_ACTION_SVC_AUTO,
        kb.REPLY_ACTION_SVC_REFRESH,
        kb.REPLY_ACTION_SVC_DELETE,
        # Keep admin hub stack when opening list screens / group hubs
        kb.REPLY_ACTION_ADM_HUB_OPS,
        kb.REPLY_ACTION_ADM_HUB_PEOPLE,
        kb.REPLY_ACTION_ADM_HUB_PRODUCT,
        kb.REPLY_ACTION_ADM_HUB_MARKETING,
        kb.REPLY_ACTION_ADM_HUB_SYSTEM,
        kb.REPLY_ACTION_ADMIN,
        kb.REPLY_ACTION_ADMIN_DASH,
        kb.REPLY_ACTION_ADMIN_REPORTS,
        kb.REPLY_ACTION_ADMIN_ORDERS,
        kb.REPLY_ACTION_ADMIN_PAYMENTS,
        kb.REPLY_ACTION_ADMIN_TICKETS,
        kb.REPLY_ACTION_ADMIN_USERS,
        kb.REPLY_ACTION_ADMIN_RESELLERS,
        kb.REPLY_ACTION_ADMIN_SETTINGS,
        kb.REPLY_ACTION_ADMIN_BROADCAST,
        kb.REPLY_ACTION_ADMIN_BACKUP,
        kb.REPLY_ACTION_ADMIN_PLANS,
        kb.REPLY_ACTION_ADMIN_PG,
        kb.REPLY_ACTION_ADMIN_PREVIEW,
        kb.REPLY_ACTION_ADM_USERS_LIST,
        kb.REPLY_ACTION_ADM_USERS_SEARCH,
        kb.REPLY_ACTION_ADM_USERS_WEB,
        kb.REPLY_ACTION_ADM_RES_LIST,
        kb.REPLY_ACTION_ADM_RES_SEARCH,
        kb.REPLY_ACTION_ADM_RES_APPS,
        kb.REPLY_ACTION_ADM_RES_ADD,
        kb.REPLY_ACTION_LOYALTY,
        kb.REPLY_ACTION_LOY_REFERRAL,
        kb.REPLY_ACTION_LOY_POINTS,
        kb.REPLY_ACTION_LOY_REWARDS,
        kb.REPLY_ACTION_LOY_WHEEL,
        kb.REPLY_ACTION_LOY_HISTORY,
        kb.REPLY_ACTION_REFERRAL,
        kb.REPLY_ACTION_ADMIN_LOYALTY,
        kb.REPLY_ACTION_ADM_LOY_OVERVIEW,
        kb.REPLY_ACTION_ADM_LOY_RULES,
        kb.REPLY_ACTION_ADM_LOY_REWARDS,
        kb.REPLY_ACTION_ADM_LOY_TIERS,
        kb.REPLY_ACTION_ADM_LOY_SETTINGS,
        kb.REPLY_ACTION_ADM_LOY_REF_TEXT,
    }
    if not preserve_state:
        await state.clear()

    if action == kb.REPLY_ACTION_HOME:
        await nav.clear_nav(state)
        await render_home(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            ui=ui,
            effective_role=role,
        )
        return

    if action == kb.REPLY_ACTION_BACK:
        await handle_back(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            reseller_profile_id=reseller_profile_id,
        )
        return

    if action in _OWNER_ONLY_REPLY_ACTIONS:
        if not await _deny_unless_owner(
            message, session, db_user, is_reseller_bot=is_reseller_bot
        ):
            return

    if action == kb.REPLY_ACTION_MINIAPP:
        # Legacy ReplyKeyboard «مینی‌اپ» label — Mini App is MenuButton Open only.
        await message.answer(
            "مینی‌اپ را از دکمهٔ Open کنار کادر پیام باز کنید."
        )
        return

    if action == kb.REPLY_ACTION_SHOP:
        await open_shop_list(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_SHOP_CUSTOM:
        bubble = await message.answer("⏳")
        from app.bot.handlers import shop as shop_h

        cb = _SoftCallback(bubble, "shop:kind:custom")
        await shop_h.shop_kind_custom(cb, session, state)
    elif action == kb.REPLY_ACTION_SHOP_WHOLESALE:
        bubble = await message.answer("⏳")
        from app.bot.handlers import shop as shop_h

        cb = _SoftCallback(bubble, "shop:kind:wholesale")
        await shop_h.shop_kind_wholesale(cb, session, state)
    elif action == kb.REPLY_ACTION_SERVICES:
        await open_services_list(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_WALLET:
        await open_wallet_home(message, session, db_user, state)
    elif action == kb.REPLY_ACTION_WALLET_TOPUP:
        await open_wallet_topup(message, session, state)
    elif action == kb.REPLY_ACTION_WALLET_TX:
        await open_wallet_tx(message, session, db_user)
    elif action == kb.REPLY_ACTION_SUPPORT:
        await open_support_home(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_SUPPORT_NEW:
        await open_support_new(message, session, state)
    elif action == kb.REPLY_ACTION_SUPPORT_LIST:
        await open_support_list(message, session, db_user)
    elif action in {kb.REPLY_ACTION_LOYALTY}:
        await open_loyalty_home(message, session, db_user, state)
    elif action in {kb.REPLY_ACTION_REFERRAL, kb.REPLY_ACTION_LOY_REFERRAL}:
        from app.bot.handlers.loyalty import open_loyalty_referral_message

        await open_loyalty_referral_message(message, session, db_user, state, push=False)
    elif action == kb.REPLY_ACTION_LOY_POINTS:
        from app.bot.handlers.loyalty import open_loyalty_points_message

        await open_loyalty_points_message(message, session, db_user)
        await nav.set_loy_step(state, "points")
    elif action == kb.REPLY_ACTION_LOY_REWARDS:
        from app.bot.handlers.loyalty import open_loyalty_rewards_message

        await open_loyalty_rewards_message(message, session, db_user)
        await nav.set_loy_step(state, "rewards")
    elif action == kb.REPLY_ACTION_LOY_WHEEL:
        from app.bot.handlers.loyalty import open_loyalty_wheel_message

        await open_loyalty_wheel_message(message, session, db_user)
        await nav.set_loy_step(state, "wheel")
    elif action == kb.REPLY_ACTION_LOY_HISTORY:
        from app.bot.handlers.loyalty import open_loyalty_history_message

        await open_loyalty_history_message(message, session, db_user)
        await nav.set_loy_step(state, "history")
    elif action == kb.REPLY_ACTION_ADMIN_LOYALTY:
        if is_reseller_bot:
            await _refuse_admin(message)
        else:
            await open_admin_loyalty_hub(
                message,
                session,
                db_user,
                state,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )
    elif action == kb.REPLY_ACTION_ADM_LOY_OVERVIEW:
        from app.bot.handlers.loyalty import open_admin_loyalty_overview

        await open_admin_loyalty_overview(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADM_LOY_RULES:
        from app.bot.handlers.loyalty import open_admin_loyalty_rules

        await open_admin_loyalty_rules(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADM_LOY_REWARDS:
        from app.bot.handlers.loyalty import open_admin_loyalty_rewards

        await open_admin_loyalty_rewards(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADM_LOY_TIERS:
        from app.bot.handlers.loyalty import open_admin_loyalty_tiers

        await open_admin_loyalty_tiers(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADM_LOY_SETTINGS:
        from app.bot.handlers.loyalty import open_admin_loyalty_settings

        await open_admin_loyalty_settings(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADM_LOY_REF_TEXT:
        from app.bot.handlers.loyalty import open_admin_loyalty_ref_text

        await open_admin_loyalty_ref_text(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_RESELLER_APPLY:
        await open_reseller_apply(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_RESELLER:
        # Sticky preview flag survives nested user taps (shop/wallet) that
        # change NAV_LEVEL away from NAV_USER_PREVIEW — still heal chrome.
        if await nav.is_user_preview_active(state):
            from app.bot.nav_chrome import heal_main_reply
            from app.bot.nav_inline import clear_nav_panel

            await clear_nav_panel(state)
            await nav.set_user_preview_active(state, False)
            await heal_main_reply(
                message,
                session,
                db_user,
                text="به پنل نماینده برگشتید.",
                is_reseller_bot=True,
                reseller_owner_id=reseller_owner_id,
                as_user=False,
                ui=ui,
            )
        await open_reseller_home(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_CREDS:
        await open_reseller_creds(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADMIN:
        # Exit user-preview: restore admin ReplyKeyboard and force a fresh
        # panel at the bottom (do not edit a buried admin hub above preview).
        # Use sticky flag — nested preview taps leave NAV_LEVEL ≠ user_preview.
        if await nav.is_user_preview_active(state):
            from app.bot.nav_chrome import heal_main_reply
            from app.bot.nav_inline import clear_nav_panel

            await clear_nav_panel(state)
            await nav.set_user_preview_active(state, False)
            await heal_main_reply(
                message,
                session,
                db_user,
                text="به پنل ادمین برگشتید.",
                is_reseller_bot=False,
                reseller_owner_id=None,
                as_user=False,
                ui=ui,
            )
        await open_admin_home(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_HUB_OPS:
        await open_admin_ops_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_HUB_PEOPLE:
        await open_admin_people_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_HUB_PRODUCT:
        await open_admin_product_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_HUB_MARKETING:
        from app.bot.handlers.nav_marketing import open_admin_marketing_hub

        await open_admin_marketing_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_HUB_SYSTEM:
        await open_admin_system_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_PREVIEW:
        await open_user_preview(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_RES_PREVIEW:
        if not is_reseller_bot:
            await message.answer(feedback.PREVIEW_ONLY_ON_SHOP_BOT)
            return
        await open_user_preview(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADMIN_DASH:
        await _soft_admin(
            message, session, db_user, "adm:dash", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_REPORTS:
        await _soft_admin(
            message, session, db_user, "adm:reports:week", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_ORDERS:
        await _soft_admin(
            message, session, db_user, "adm:orders", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_PAYMENTS:
        await _soft_admin(
            message, session, db_user, "adm:payments", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_TICKETS:
        await _soft_admin(
            message, session, db_user, "adm:tickets", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_PLANS:
        await open_admin_plans_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_PLANS_AUD_USERS:
        await present_admin_plans_audience(
            message,
            session,
            db_user,
            state,
            audience="users",
            is_reseller_bot=is_reseller_bot,
        )
    elif action == kb.REPLY_ACTION_ADM_PLANS_AUD_RESELLERS:
        await present_admin_plans_audience(
            message,
            session,
            db_user,
            state,
            audience="resellers",
            is_reseller_bot=is_reseller_bot,
        )
    elif action == kb.REPLY_ACTION_ADM_PLANS_ADD:
        data = await state.get_data()
        aud = data.get("_adm_plans_aud") or "users"
        if aud not in {"users", "resellers"}:
            await open_admin_plans_hub(
                message, session, db_user, state, is_reseller_bot=is_reseller_bot
            )
            return
        from app.bot.nav_inline import (
            admin_plans_add_type_hub_keyboard,
            present_inline_only,
        )

        ui = await get_all_settings(session)
        if state is not None:
            await nav.set_nav_level(
                state, nav.NAV_ADMIN_PLANS_ADD_TYPE, push=True
            )
        await present_inline_only(
            message,
            text=feedback.PICK_PLAN_TYPE,
            inline=admin_plans_add_type_hub_keyboard(str(aud), ui),
        )
        return

    elif action == kb.REPLY_ACTION_ADM_PLANS_CATEGORIES:
        if not await _deny_unless_owner(
            message, session, db_user, is_reseller_bot=is_reseller_bot
        ):
            return
        from app.bot.handlers.plan_catalog_manage import open_categories_manage

        await open_categories_manage(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
    elif action == kb.REPLY_ACTION_ADM_PLANS_ADDONS:
        if not await _deny_unless_owner(
            message, session, db_user, is_reseller_bot=is_reseller_bot
        ):
            return
        from app.bot.handlers.plan_catalog_manage import open_addons_manage

        await open_addons_manage(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
    elif action in {
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL,
        kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL,
        kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS,
    }:
        kind_map = {
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED: "fixed",
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_CUSTOM: "custom",
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_TRIAL: "trial",
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_WHOLESALE: "wholesale",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED: "fixed",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG: "payg",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL: "addon_volume",
            kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS: "addon_users",
        }
        kind = kind_map[action]
        data = await state.get_data()
        aud = data.get("_adm_plans_aud") or (
            "resellers"
            if action
            in {
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_FIXED,
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_PAYG,
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_VOL,
                kb.REPLY_ACTION_ADM_PLANS_KIND_RES_ADDON_USERS,
            }
            else "users"
        )
        level = await nav.get_nav_level(state)
        if level == nav.NAV_ADMIN_PLANS_ADD_TYPE:
            from app.bot.handlers.admin_plans import open_add_kind_action

            await open_add_kind_action(message, session, db_user, state, aud, kind)
        else:
            await state.update_data(_adm_plans_aud=aud, _adm_plans_kind=kind)
            from app.bot.handlers.admin_plans import open_kind_screen

            await open_kind_screen(message, session, aud, kind)
    elif action == kb.REPLY_ACTION_ADMIN_PG:
        await open_pg_home(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADMIN_USERS:
        await open_admin_users_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_SETTINGS:
        await open_admin_settings_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_BROADCAST:
        await open_admin_broadcast_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_RESELLERS:
        await open_admin_resellers_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADMIN_BACKUP:
        await open_admin_backup_hub(
            message, session, db_user, state, is_reseller_bot=is_reseller_bot
        )
    elif action in {"adm_plan_add", kb.REPLY_ACTION_ADM_PLAN_ADD}:
        await _soft_admin(
            message, session, db_user, "adm:plan:add", state, is_reseller_bot=is_reseller_bot
        )
    elif action in {"adm_plan_custom", kb.REPLY_ACTION_ADM_PLAN_CUSTOM}:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:plans:kind:users:custom",
            state,
            is_reseller_bot=is_reseller_bot,
        )
    elif action in {"adm_plan_trial", kb.REPLY_ACTION_ADM_PLAN_TRIAL}:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:plans:kind:users:trial",
            state,
            is_reseller_bot=is_reseller_bot,
        )
    elif action == "backup_create":
        # Phase 2 default: without .env
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:backup:create:noenv",
            state,
            is_reseller_bot=is_reseller_bot,
        )
    elif action == "backup_create_env":
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:backup:create:env",
            state,
            is_reseller_bot=is_reseller_bot,
        )
    elif action == "backup_create_noenv":
        # Legacy reply label — same as safe default
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:backup:create:noenv",
            state,
            is_reseller_bot=is_reseller_bot,
        )
    elif action == "backup_upload":
        await _soft_admin(
            message, session, db_user, "adm:backup:upload", state, is_reseller_bot=is_reseller_bot
        )
    elif action == "backup_refresh":
        await open_admin_backup_hub(
            message, session, db_user, state, push=False, is_reseller_bot=is_reseller_bot
        )
    elif action.startswith("bc_aud_"):
        aud = action.replace("bc_aud_", "", 1)
        await _soft_admin(
            message,
            session,
            db_user,
            f"adm:broadcast:aud:{aud}",
            state,
            is_reseller_bot=is_reseller_bot,
        )
    elif action == kb.REPLY_ACTION_PG_STATS:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:pg:stats",
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_PG_USERS:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:pg:users",
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_PG_CREATE:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:pg:create",
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_PG_SEARCH:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:pg:search",
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_PG_NODES:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:pg:nodes",
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_PG_GROUP:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:pg:group",
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_PG_TEMPLATE:
        await _soft_admin(
            message,
            session,
            db_user,
            "adm:pg:template",
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )
    elif action == kb.REPLY_ACTION_ADM_USERS_LIST:
        await _soft_admin(
            message, session, db_user, "adm:users:list:0", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_USERS_SEARCH:
        await _soft_admin(
            message, session, db_user, "adm:users:search", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_USERS_WEB:
        await _soft_admin(
            message, session, db_user, "adm:users:webhint", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_RES_LIST:
        await _soft_admin(
            message, session, db_user, "adm:resellers:list:0", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_RES_SEARCH:
        await _soft_admin(
            message, session, db_user, "adm:resellers:search", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_RES_APPS:
        await _soft_admin(
            message, session, db_user, "adm:resapp:list", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_RES_ADD:
        await _soft_admin(
            message, session, db_user, "adm:resellers:add", state, is_reseller_bot=is_reseller_bot
        )
    elif action == kb.REPLY_ACTION_ADM_ST_PANEL:
        if not await _deny_unless_owner(
            message, session, db_user, is_reseller_bot=is_reseller_bot
        ):
            return
        from app.bot.handlers.admin_settings import _panel_settings_url

        url = await _panel_settings_url(session, for_shop=False)
        await message.answer(
            "🌐 <b>تنظیمات کامل وب‌پنل</b>\n"
            "ظاهر ربات، رنگ دکمه‌ها، گزارش روزانه، لینک‌ها و متن‌های بلند:\n"
            f"<code>{url}</code>"
        )
    elif action == kb.REPLY_ACTION_RES_ST_PANEL:
        from app.services.reseller_access import load_reseller_actor
        from app.services.resellers import get_reseller_panel_base_url, has_bot_perm

        if not is_reseller_bot:
            await open_reseller_creds(
                message,
                session,
                db_user,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
        owner_id, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        if not owner_id or not profile or not has_bot_perm(profile, "shop_settings"):
            await message.answer(feedback.NO_SHOP_SETTINGS_ACCESS)
            return
        try:
            base = (await get_reseller_panel_base_url(session) or "").rstrip("/")
        except Exception:
            base = ""
        url = f"{base}/shop-settings" if base else "/shop-settings"
        await message.answer(
            "🌐 <b>تنظیمات کامل فروشگاه در وب‌پنل</b>\n"
            "ظاهر، رنگ، گزارش روزانه و متن‌های بلند:\n"
            f"<code>{url}</code>\n"
            "فقط همین فروشگاه — بدون دسترسی به تنظیمات پلتفرم."
        )
    elif action.startswith("adm_st_"):
        sec = action.replace("adm_st_", "", 1)
        # Legacy reply keyboards used «سرویس و دسترسی»; ops fields moved to access.
        if sec == "service":
            sec = "access"
        await _soft_admin(
            message,
            session,
            db_user,
            f"adm:st:sec:{sec}",
            state,
            is_reseller_bot=is_reseller_bot,
        )
    elif action.startswith("res_"):
        await _soft_reseller(
            message,
            session,
            db_user,
            action,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action in {
        kb.REPLY_ACTION_SVC_LINK,
        kb.REPLY_ACTION_SVC_GUIDE,
        kb.REPLY_ACTION_SVC_CANCEL,
        kb.REPLY_ACTION_SVC_RENEW,
        kb.REPLY_ACTION_SVC_ADDON,
        kb.REPLY_ACTION_SVC_AUTO,
        kb.REPLY_ACTION_SVC_REFRESH,
        kb.REPLY_ACTION_SVC_DELETE,
    }:
        data = await state.get_data()
        svc_id = data.get(nav.SERVICE_ID)
        if not svc_id:
            # Stale reply keyboard after restart (MemoryStorage empty) — reopen
            # services and re-install the main ReplyKeyboard once (no dead-end).
            await open_services_list(
                message,
                session,
                db_user,
                state,
                push=False,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
        from app.bot.handlers import services as svc_h

        bubble = await message.answer("⏳")
        if action == kb.REPLY_ACTION_SVC_LINK:
            cb_data = f"svc:link:{int(svc_id)}"
            fn = svc_h.svc_link
        elif action == kb.REPLY_ACTION_SVC_GUIDE:
            cb_data = f"guide:svc:{int(svc_id)}"
            fn = svc_h.svc_guide
        elif action == kb.REPLY_ACTION_SVC_CANCEL:
            cb_data = f"svc:cancel:{int(svc_id)}"
            fn = svc_h.svc_cancel
        elif action == kb.REPLY_ACTION_SVC_RENEW:
            cb_data = f"svc:renew:{int(svc_id)}"
            fn = svc_h.svc_renew
        elif action == kb.REPLY_ACTION_SVC_ADDON:
            cb_data = f"svc:addon:{int(svc_id)}"
            fn = svc_h.svc_addon
        elif action == kb.REPLY_ACTION_SVC_AUTO:
            cb_data = f"svc:auto:{int(svc_id)}"
            fn = svc_h.svc_auto
        elif action == kb.REPLY_ACTION_SVC_DELETE:
            cb_data = f"svc:delask:{int(svc_id)}"
            fn = svc_h.svc_delete_ask
        else:
            cb_data = f"svc:view:{int(svc_id)}"
            fn = svc_h.svc_view
        cb = _SoftCallback(bubble, cb_data)
        try:
            await fn(cb, session, db_user, state)
        except TypeError:
            await fn(cb, session, db_user)
    elif action.startswith("pay_"):
        await _handle_pay_action(
            message,
            session,
            db_user,
            state,
            action,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    elif action.startswith("topup_"):
        await _handle_topup_action(message, session, db_user, state, action)

@router.message(F.text.func(kb.is_cancel_text))
async def global_cancel_restore(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Fallback انصراف: outside FSM → home; inside FSM → defer to state handlers."""
    from aiogram.dispatcher.event.bases import SkipHandler

    current = await state.get_state()
    if current is not None:
        # State-specific handlers (shop/wallet/admin/…) own cancel + context restore
        raise SkipHandler()
    await restore_main_reply(
        message,
        session,
        db_user,
        text="🏠 منوی اصلی",
        state=state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
