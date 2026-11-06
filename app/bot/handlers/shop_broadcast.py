"""Shop-bot group messages: same stages as the owner broadcast, one shop's customers only."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.reseller_hub_groups import back_callback_for
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.broadcast import clear_broadcast_history, delete_broadcast_log, list_broadcast_history
from app.services.broadcast_queue import STATUS_LABELS, cancel_broadcast
from app.services.redact import user_safe_error
from app.services.shop_broadcast import (
    SHOP_AUDIENCE_LABELS,
    clean_shop_broadcast_text,
    new_shop_request_key,
    queue_shop_broadcast,
)
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="shop_broadcast")

CB_HUB = "shp:bc:hub"
CB_AUD_ALL = "shp:bc:aud:all"
CB_HIST = "shp:bc:hist"
CB_CONFIRM = "shp:bc:go"
CB_CANCEL_PREFIX = "shp:bc:x:"
CB_DELETE_PREFIX = "shp:bc:d:"
CB_CLEAR = "shp:bc:clr"
HISTORY_LIMIT = 8


class ShopBroadcastStates(StatesGroup):
    waiting_text = State()
    confirm = State()


async def _shop_id(
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


def _hub_keyboard(ui: dict) -> InlineKeyboardMarkup:
    from app.bot.nav_inline import _ikb, _pack_hub_keyboard

    buttons = [
        _ikb(
            "همهٔ مشتریان",
            callback_data=CB_AUD_ALL,
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
        snippet = (row.text or "")[:40]
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

    shop_id = await _shop_id(
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
        inline=_hub_keyboard(ui),
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
    shop_id = await _shop_id(
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


@router.callback_query(F.data == CB_AUD_ALL)
async def cb_audience(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.bot.nav_input import ask_text

    shop_id = await _shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if shop_id is None:
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    await callback.answer()
    await state.update_data(
        shop_broadcast_audience="all",
        shop_broadcast_shop_id=shop_id,
        shop_broadcast_request_key=new_shop_request_key(),
    )
    label = SHOP_AUDIENCE_LABELS["all"]
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

    shop_id = await _shop_id(
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
    await state.set_state(ShopBroadcastStates.confirm)
    ui = await get_all_settings(session)
    preview = body if len(body) <= 500 else body[:500] + "…"
    await message.answer(
        f"تأیید ارسال به <b>{SHOP_AUDIENCE_LABELS['all']}</b>؟\n\n{preview}",
        reply_markup=_confirm_keyboard(ui),
    )


@router.callback_query(F.data == CB_CONFIRM, ShopBroadcastStates.confirm)
async def cb_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _shop_id(
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
            already_cleaned=True,
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
    shop_id = await _shop_id(
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
    shop_id = await _shop_id(
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
    shop_id = await _shop_id(
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
    shop_id = await _shop_id(
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
