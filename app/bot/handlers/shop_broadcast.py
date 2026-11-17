"""Shop-bot group messages: same stages as the owner broadcast, one shop's customers only."""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.broadcast_segments import SHOP_LIST, SHOP_PICK, TARGETED_LABEL, TITLE, targeted_keyboard
from app.bot.reseller_hub_groups import back_callback_for
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.broadcast import clear_broadcast_history, delete_broadcast_log, list_broadcast_history
from app.services.broadcast_audience import describe, make_spec
from app.services.broadcast_queue import STATUS_LABELS, cancel_broadcast
from app.bot.message_button_picker import ask_for_buttons, register_flow
from app.services.message_buttons import ButtonInput, broadcast_buttons, preview_keyboard
from app.services.redact import user_safe_error
from app.services.shop_broadcast import (
    clean_shop_broadcast_text,
    new_shop_request_key,
    queue_shop_broadcast,
    shop_broadcast_html_snippet,
)
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="shop_broadcast")

CB_HUB = "shp:bc:hub"
CB_AUD_ALL = f"{SHOP_PICK}all"
CB_HIST = "shp:bc:hist"
CB_CONFIRM = "shp:bc:go"
CB_CANCEL_PREFIX = "shp:bc:x:"
CB_DELETE_PREFIX = "shp:bc:d:"
CB_CLEAR = "shp:bc:clr"
FLOW = "shop_broadcast"
HISTORY_LIMIT = 8


class ShopBroadcastStates(StatesGroup):
    waiting_text = State()
    confirm = State()


async def resolve_shop_id(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> int | None:
    from app.bot.auth import can_shop_feature
    from app.services.reseller_access import load_reseller_actor

    if not is_reseller_bot:
        return None
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        return None
    if not can_shop_feature("broadcast", profile=profile, db_user=db_user):
        return None
    return int(owner_id)


async def shop_has_campaigns(
    session: AsyncSession, db_user: BotUser, *, reseller_owner_id: int | None
) -> bool:
    """Whether the staff member may see the shop's campaign list (``campaigns`` permission)."""
    from app.bot.auth import can_shop_feature
    from app.services.reseller_access import load_reseller_actor

    _owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
    )
    return bool(profile) and can_shop_feature("campaigns", profile=profile, db_user=db_user)


def _hub_keyboard(ui: dict, *, campaigns: bool = True) -> InlineKeyboardMarkup:
    from app.bot.nav_inline import _ikb, _pack_hub_keyboard, _reseller_entry_buttons, _t

    buttons = [
        _ikb(
            "همهٔ مشتریان",
            callback_data=CB_AUD_ALL,
            ui=ui,
            style_id="res_broadcast",
            style_fallback="primary",
        ),
        _ikb(
            TARGETED_LABEL,
            callback_data=SHOP_LIST,
            ui=ui,
            style_id="res_broadcast",
            style_fallback="primary",
        ),
        _ikb(
            "تاریخچه ارسال",
            callback_data=CB_HIST,
            ui=ui,
            style_id="res_broadcast",
            style_fallback="secondary",
        ),
    ]
    if campaigns:
        # The campaign list (automatic and earlier campaigns) lives under group messages.
        buttons.extend(_reseller_entry_buttons([("res_campaigns", _t(ui, "btn_res_campaigns"))], ui, {}))
    return _pack_hub_keyboard(buttons, ui, back_callback=back_callback_for("res_broadcast"))


def _confirm_keyboard(ui: dict) -> InlineKeyboardMarkup:
    from app.bot.nav_inline import _ikb

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_ikb("✅ تأیید و ارسال", callback_data=CB_CONFIRM, ui=ui, style_fallback="primary")],
            [_ikb("انصراف", callback_data=CB_HUB, ui=ui, style_fallback="secondary")],
        ]
    )


def _history_keyboard(rows, ui: dict) -> InlineKeyboardMarkup:
    from app.bot.nav_inline import _ikb

    lines: list[list[InlineKeyboardButton]] = []
    for row in rows:
        if row.status in ("queued", "running"):
            lines.append(
                [
                    _ikb(
                        f"توقف #{row.id}",
                        callback_data=f"{CB_CANCEL_PREFIX}{row.id}",
                        ui=ui,
                        style_fallback="danger",
                    )
                ]
            )
        else:
            lines.append(
                [
                    _ikb(
                        f"حذف #{row.id}",
                        callback_data=f"{CB_DELETE_PREFIX}{row.id}",
                        ui=ui,
                        style_fallback="danger",
                    )
                ]
            )
    if rows:
        lines.append(
            [_ikb("پاک‌کردن تاریخچه", callback_data=CB_CLEAR, ui=ui, style_fallback="danger")]
        )
    lines.append([_ikb("بازگشت", callback_data=CB_HUB, ui=ui, style_fallback="secondary")])
    return InlineKeyboardMarkup(inline_keyboard=lines)


def _history_text(rows) -> str:
    if not rows:
        return "هنوز پیامی ارسال نشده."
    lines = []
    for row in rows:
        status = STATUS_LABELS.get(row.status, row.status)
        snippet = shop_broadcast_html_snippet(row.text, limit=40)
        lines.append(f"#{row.id} · {status} · {row.ok_count}/{row.total}\n<code>{snippet}</code>")
    return "تاریخچه پیام گروهی:\n\n" + "\n\n".join(lines)


async def _show_history(message: Message, session: AsyncSession, *, shop_id: int) -> None:
    rows = await list_broadcast_history(session, limit=HISTORY_LIMIT, shop_id=shop_id)
    ui = await get_all_settings(session)
    await message.edit_text(_history_text(rows), reply_markup=_history_keyboard(rows, ui))


async def open_shop_broadcast_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    push: bool = True,
) -> None:
    from app.bot.nav_chrome import with_caption
    from app.bot.nav_inline import present_nav_panel
    from app.bot.screen_registry import screen_caption, screen_heading

    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await message.answer("دسترسی به پیام گروهی ندارید.")
        return
    if state is not None:
        await state.clear()
    ui = await get_all_settings(session)
    body = with_caption(screen_heading(ui, "r.broadcast"), screen_caption(ui, "r.broadcast"))
    await present_nav_panel(
        message,
        text=body or "📢 پیام گروهی به مشتریان فروشگاه",
        inline=_hub_keyboard(
            ui, campaigns=await shop_has_campaigns(session, db_user, reseller_owner_id=reseller_owner_id)
        ),
        state=state,
        push=push,
    )


@router.callback_query(F.data == CB_HUB)
async def cb_hub(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await open_shop_broadcast_hub(
            callback.message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            push=False,
        )


@router.callback_query(F.data == SHOP_LIST)
async def cb_segments(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        markup = targeted_keyboard(shop_id=shop_id, pick_prefix=SHOP_PICK, back=CB_HUB)
        await safe_edit_text(callback.message, TITLE, reply_markup=markup)


def _audience_label(segment: str, shop_id: int) -> str:
    """The audience as the confirm card names it; ValueError for a segment this shop may not use."""
    return describe(make_spec(segment, shop_id=shop_id))


@router.callback_query(F.data.startswith(SHOP_PICK))
async def cb_audience(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.nav_input import ask_text

    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    segment = (callback.data or "")[len(SHOP_PICK):]
    try:
        label = html.escape(_audience_label(segment, shop_id))
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    await state.update_data(
        shop_broadcast_audience=segment,
        shop_broadcast_shop_id=shop_id,
        shop_broadcast_request_key=new_shop_request_key(),
    )
    if callback.message:
        await callback.message.edit_text(
            f"📢 مخاطب: <b>{label}</b>\nمتن پیام را بفرستید.\nبرای لغو: انصراف"
        )
    await ask_text(
        callback,
        state,
        prompt="متن پیام:",
        cancel_code="shp_bc",
        fsm_state=ShopBroadcastStates.waiting_text,
        edit=False,
    )


@router.message(ShopBroadcastStates.waiting_text)
async def on_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot import keyboards as kb
    from app.bot.nav_input import try_legacy_cancel

    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    data = await state.get_data()
    if shop_id is None or data.get("shop_broadcast_shop_id") != shop_id:
        await state.clear()
        await message.answer("دسترسی به پیام گروهی ندارید.")
        return
    if kb.is_cancel_text(message.text):
        if await try_legacy_cancel(message, state, session, db_user):
            return
        await open_shop_broadcast_hub(
            message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    try:
        body = clean_shop_broadcast_text(message.text)
    except ValueError as exc:
        await message.answer(f"⚠️ {user_safe_error(exc)}")
        return
    await state.update_data(shop_broadcast_text=body)
    await ask_for_buttons(message, state, flow=FLOW, shop_id=shop_id)


async def _confirm_with_buttons(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    buttons: list[ButtonInput],
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    **_: object,
) -> None:
    """After the buttons step: the message with its buttons as customers will get it, then «تأیید»."""
    message = callback.message
    shop_id = await resolve_shop_id(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    data = await state.get_data()
    if not isinstance(message, Message) or shop_id is None or data.get("shop_broadcast_shop_id") != shop_id:
        await state.clear()
        return
    try:
        stored = await broadcast_buttons(session, buttons, shop_id=shop_id)
    except ValueError as exc:
        await state.clear()
        await message.answer(f"⚠️ {user_safe_error(exc)}")
        return
    body = str(data.get("shop_broadcast_text") or "")
    await state.update_data(shop_broadcast_buttons=stored)
    await state.set_state(ShopBroadcastStates.confirm)
    ui = await get_all_settings(session)
    preview = shop_broadcast_html_snippet(body, limit=500) + ("…" if len(body) > 500 else "")
    rows = preview_keyboard(stored)
    if rows:
        # The buttons exactly as customers will get them, under the message preview.
        await message.answer(preview, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    segment = str(data.get("shop_broadcast_audience") or "all")
    await message.answer(
        f"تأیید ارسال به <b>{html.escape(_audience_label(segment, shop_id))}</b>؟\n\n{preview}",
        reply_markup=_confirm_keyboard(ui),
    )


register_flow(FLOW, _confirm_with_buttons)


@router.callback_query(F.data == CB_CONFIRM, ShopBroadcastStates.confirm)
async def cb_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    data = await state.get_data()
    if shop_id is None or data.get("shop_broadcast_shop_id") != shop_id:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        await state.clear()
        return
    text = data.get("shop_broadcast_text") or ""
    request_key = data.get("shop_broadcast_request_key")
    await callback.answer()
    try:
        log = await queue_shop_broadcast(
            session,
            shop_id=shop_id,
            text=text,
            created_by=str(db_user.telegram_id),
            request_key=request_key,
            buttons=data.get("shop_broadcast_buttons"),
            audience=make_spec(str(data.get("shop_broadcast_audience") or "all"), shop_id=shop_id),
        )
    except ValueError as exc:
        await session.rollback()
        if callback.message:
            await callback.message.answer(f"⚠️ {user_safe_error(exc)}")
        await state.clear()
        return
    await state.clear()
    if callback.message:
        await callback.message.answer(
            f"✅ در صف ارسال قرار گرفت\nمخاطبان: {log.total}\n"
            "ارسال به‌تدریج انجام می‌شود؛ پیشرفت در همین بخش و پنل وب دیده می‌شود."
        )
        await open_shop_broadcast_hub(
            callback.message,
            session,
            db_user,
            state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            push=False,
        )


@router.callback_query(F.data == CB_HIST)
async def cb_history(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _show_history(callback.message, session, shop_id=shop_id)


def _parse_log_id(data: str, prefix: str) -> int | None:
    raw = data[len(prefix) :]
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


@router.callback_query(F.data.startswith(CB_CANCEL_PREFIX))
async def cb_cancel_row(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    log_id = _parse_log_id(callback.data or "", CB_CANCEL_PREFIX)
    if shop_id is None or log_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    ok = await cancel_broadcast(session, log_id, shop_id=shop_id)
    await callback.answer("متوقف شد" if ok else "فعال نیست", show_alert=True)
    if callback.message:
        await _show_history(callback.message, session, shop_id=shop_id)


@router.callback_query(F.data.startswith(CB_DELETE_PREFIX))
async def cb_delete_row(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    log_id = _parse_log_id(callback.data or "", CB_DELETE_PREFIX)
    if shop_id is None or log_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    ok = await delete_broadcast_log(session, log_id, shop_id=shop_id)
    await callback.answer("حذف شد" if ok else "حذف نشد", show_alert=True)
    if callback.message:
        await _show_history(callback.message, session, shop_id=shop_id)


@router.callback_query(F.data == CB_CLEAR)
async def cb_clear(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await resolve_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    n = await clear_broadcast_history(session, shop_id=shop_id)
    await callback.answer(f"{n} رکورد حذف شد", show_alert=True)
    if callback.message:
        ui = await get_all_settings(session)
        await callback.message.edit_text(
            "هنوز پیامی ارسال نشده.", reply_markup=_history_keyboard([], ui)
        )
