"""«افزودن سرویس قبلی» from the bot: staff attach a customer's existing panel service.

Same steps and the same service functions as the web edit screen: pick link or panel username,
send it, see the preview, confirm, give the reason. The platform Owner uses it from the admin
bot, a shop operator from the shop bot; the shop always comes from the bot the update arrived
on and the acting person, never from callback data. The typed link is a bearer secret: it is
deleted from the chat once read and kept only in this person's FSM data until the step ends.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import resolve_bot_principal_bridge
from app.bot.handlers.action_reason import ask_reason, take_reason
from app.bot.handlers.subscription_lookup import delete_typed_link
from app.bot.nav_input import ask_text, finish_text_step, try_legacy_cancel
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.action_reasons import bot_actor
from app.services.bot_principal_identity import shop_bot_panel_staff
from app.services.formatting import format_message, info_block, kv_line
from app.services.reseller_access import load_reseller_actor
from app.services.service_import import ImportOutcome
from app.services.service_import_assign import (
    KIND_LINK,
    KIND_USERNAME,
    AssignRefused,
    AssignRequest,
    ServicePreview,
    assign_existing_service,
    may_assign_existing_services,
    parse_lookup,
    preview_existing_service,
)
from app.services.shop_scope import ShopScopeError, is_platform_admin

logger = logging.getLogger(__name__)
router = Router(name="service_import_assign_bot")

PREFIX = "sia"
PLATFORM_CANCEL_CODE = "adm_usr"
SHOP_CANCEL_CODE = "rs_svc"
KEY_USER = "sia_uid"
KEY_KIND = "sia_kind"
KEY_VALUE = "sia_value"
KEY_PANEL_ID = "sia_pid"
FLOW_KEYS = (KEY_USER, KEY_KIND, KEY_VALUE, KEY_PANEL_ID)
KIND_CODES = {"l": KIND_LINK, "u": KIND_USERNAME}
TITLE = "➕ افزودن سرویس قبلی"
INTRO = (
    "سرویسی که مشتری از قبل در پنل دارد با لینک اشتراک یا نام کاربری پنل به «سرویس‌های من» او "
    "وصل می‌شود. در پنل چیزی تغییر نمی‌کند؛ سرویس قابل تمدید است ولی لغو و بازگشت وجه ندارد.\n\n"
    "با کدام وارد می‌کنید؟"
)
PROMPTS = {
    KIND_LINK: "🔗 لینک اشتراک مشتری را بفرستید.\n<i>(پیام شما پس از خواندن پاک می‌شود)</i>",
    KIND_USERNAME: "👤 نام کاربری سرویس در پنل را بفرستید.",
}
ALREADY_YOURS = "این سرویس از قبل در سرویس‌های این مشتری است."
EXPIRED_STEP = "این مرحله تمام شده است. دوباره از «سرویس‌ها» شروع کنید."
REASON_LABEL = "علت افزودن سرویس"
ADDED_TEXT = "✅ سرویس <code>{username}</code> به سرویس‌های این مشتری اضافه شد."
ALREADY_TEXT = "سرویس <code>{username}</code> از قبل در سرویس‌های این مشتری است."


class AssignStates(StatesGroup):
    value = State()


@dataclass(frozen=True, slots=True)
class BotAssignContext:
    """Who acts and for which shop (``None`` = the platform), and where «بازگشت» goes."""

    staff: dict[str, Any]
    shop_id: int | None
    is_shop_bot: bool

    def services_callback(self, user_id: int) -> str:
        return f"res:svcs:{int(user_id)}" if self.is_shop_bot else f"adm:users:svcs:{int(user_id)}"

    @property
    def cancel_code(self) -> str:
        return SHOP_CANCEL_CODE if self.is_shop_bot else PLATFORM_CANCEL_CODE


async def assign_context(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> BotAssignContext | None:
    """The acting staff with the permission, or ``None``. Shop bots never act for the platform."""
    if is_reseller_bot:
        owner_id, profile = await load_reseller_actor(
            session, db_user, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
        )
        if not owner_id or profile is None:
            return None
        staff = await shop_bot_panel_staff(session, profile=profile, db_user=db_user)
        shop_id: int | None = int(owner_id)
    else:
        bridge = await resolve_bot_principal_bridge(session, db_user, is_reseller_bot=False)
        staff = dict(bridge.staff) if bridge and bridge.staff else None
        if staff is not None and not is_platform_admin(staff):
            staff = None
        shop_id = None
    if staff is None or not may_assign_existing_services(staff):
        return None
    return BotAssignContext(staff=staff, shop_id=shop_id, is_shop_bot=is_reseller_bot)


def _parse_ids(data: str | None, *, parts: int) -> list[str] | None:
    pieces = (data or "").split(":")
    if len(pieces) != parts or pieces[0] != PREFIX or not pieces[2].isdigit():
        return None
    return pieces


def _intro_markup(ctx: BotAssignContext, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔗 لینک اشتراک", callback_data=f"{PREFIX}:k:{user_id}:l"),
            InlineKeyboardButton(text="👤 نام کاربری پنل", callback_data=f"{PREFIX}:k:{user_id}:u"),
        ],
        [kb.back_button(None, ctx.services_callback(user_id))],
    ])


def _preview_text(preview: ServicePreview) -> str:
    rows = [
        kv_line("🧾", "کاربر پنل", f"<code>{html.escape(preview.panel_username)}</code>"),
        kv_line("📶", "وضعیت", html.escape(preview.status)),
        kv_line("📊", "مصرف", html.escape(preview.used)),
        kv_line("📦", "حجم", html.escape(preview.limit)),
        kv_line("📅", "انقضا", html.escape(preview.expire)),
    ]
    text = format_message(TITLE, info_block(rows))
    if preview.already_yours:
        return text + "\n\n" + ALREADY_YOURS
    return text + "\n\nاین سرویس به سرویس‌های مشتری اضافه شود؟"


def _preview_markup(ctx: BotAssignContext, user_id: int, preview: ServicePreview) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not preview.already_yours:
        rows.append([InlineKeyboardButton(text="✅ افزودن و پیام به مشتری", callback_data=f"{PREFIX}:y:{user_id}:1")])
        rows.append([InlineKeyboardButton(text="✅ افزودن بدون پیام", callback_data=f"{PREFIX}:y:{user_id}:0")])
    rows.append([kb.back_button(None, ctx.services_callback(user_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_markup(ctx: BotAssignContext, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, ctx.services_callback(user_id))]])


def _refusal_text(exc: Exception) -> str:
    if isinstance(exc, ShopScopeError):
        return exc.message
    return str(exc)


async def _forget_flow(state: FSMContext) -> None:
    await state.update_data(**dict.fromkeys(FLOW_KEYS))


@router.callback_query(F.data.regexp(rf"^{PREFIX}:o:\d+$"))
async def sia_open(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    ctx = await assign_context(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if ctx is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    user_id = int((callback.data or "").split(":")[2])
    await _forget_flow(state)
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, format_message(TITLE, INTRO), reply_markup=_intro_markup(ctx, user_id))


@router.callback_query(F.data.regexp(rf"^{PREFIX}:k:\d+:[lu]$"))
async def sia_pick_kind(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    ctx = await assign_context(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    pieces = _parse_ids(callback.data, parts=4)
    if ctx is None or pieces is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    user_id, kind = int(pieces[2]), KIND_CODES[pieces[3]]
    await state.update_data(**{KEY_USER: user_id, KEY_KIND: kind, KEY_VALUE: None, KEY_PANEL_ID: None})
    await callback.answer()
    await ask_text(
        callback, state, prompt=PROMPTS[kind], cancel_code=ctx.cancel_code, fsm_state=AssignStates.value,
        return_to=f"{PREFIX}:o:{user_id}", edit=True,
    )


@router.message(AssignStates.value)
async def sia_value_entered(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    if kb.is_cancel_text(message.text) and await try_legacy_cancel(
        message, state, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ):
        return
    data = await state.get_data()
    kind = data.get(KEY_KIND)
    if kind == KIND_LINK:
        await delete_typed_link(message)
    await state.set_state(None)
    ctx = await assign_context(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    user = await session.get(BotUser, int(data.get(KEY_USER) or 0))
    if ctx is None or user is None or kind not in KIND_CODES.values():
        await _forget_flow(state)
        await message.answer(feedback.NO_ACCESS_REPLY)
        return
    user_id = int(user.id)
    try:
        lookup = parse_lookup(kind, message.text)
        preview = await preview_existing_service(session, staff=ctx.staff, user=user, lookup=lookup)
    except (AssignRefused, PermissionError, ShopScopeError) as exc:
        await _forget_flow(state)
        text = format_message(TITLE, html.escape(_refusal_text(exc)))
        await finish_text_step(message, state, text=text, inline=_intro_markup(ctx, user_id))
        return
    await state.update_data(**{KEY_VALUE: lookup.value, KEY_PANEL_ID: preview.panel_user_id})
    await finish_text_step(
        message, state, text=_preview_text(preview), inline=_preview_markup(ctx, user_id, preview)
    )


@router.callback_query(F.data.regexp(rf"^{PREFIX}:y:\d+:[01]$"))
async def sia_confirm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    ctx = await assign_context(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    pieces = _parse_ids(callback.data, parts=4)
    if ctx is None or pieces is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    user_id, notify = int(pieces[2]), pieces[3] == "1"
    data = await state.get_data()
    # The lookup must be the one previewed in this chat, for this very customer.
    if data.get(KEY_USER) != user_id or not data.get(KEY_VALUE) or not data.get(KEY_PANEL_ID):
        await callback.answer(EXPIRED_STEP, show_alert=True)
        return
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label=REASON_LABEL)
        return
    await _assign(callback, state, session, db_user, ctx, user_id=user_id, notify=notify, reason=reason, data=data)


async def _assign(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    ctx: BotAssignContext,
    *,
    user_id: int,
    notify: bool,
    reason: str,
    data: dict[str, Any],
) -> None:
    user = await session.get(BotUser, user_id)
    if user is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    try:
        request = AssignRequest(
            lookup=parse_lookup(data.get(KEY_KIND), data.get(KEY_VALUE)),
            expected_panel_user_id=int(data[KEY_PANEL_ID]),
            reason=reason,
            notify_customer=notify,
        )
        result = await assign_existing_service(
            session, staff=ctx.staff, user=user, request=request, actor=bot_actor(db_user, shop_id=ctx.shop_id),
        )
    except (AssignRefused, PermissionError, ShopScopeError) as exc:
        text = format_message(TITLE, html.escape(_refusal_text(exc)))
    else:
        template = ALREADY_TEXT if result.outcome is ImportOutcome.ALREADY_YOURS else ADDED_TEXT
        text = format_message(TITLE, template.format(username=html.escape(result.panel_username)))
    await _forget_flow(state)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=_back_markup(ctx, user_id))
