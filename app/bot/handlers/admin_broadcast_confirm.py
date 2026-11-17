"""Platform broadcast from the bot: show the message exactly as customers will get it, then confirm.

Typing the text used to queue the broadcast at once. Now the owner is asked for buttons
(``message_button_picker``), then sees the real message with them and a confirm card with the audience
size; only «ارسال» queues it, once (the card carries a request token, so a second tap returns the
broadcast already queued).
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner_handler
from app.bot.message_button_picker import ask_for_buttons, register_flow
from app.bot.nav_inline import with_inline_back
from app.bot.nav_input import finish_text_step
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.broadcast import AUDIENCE_LABELS
from app.services.broadcast_queue import BroadcastRequest, count_platform_targets, new_request_token, queue_broadcast
from app.services.message_buttons import ButtonInput, broadcast_buttons, preview_keyboard
from app.services.redact import user_safe_error
from app.services.telegram_html import normalize_telegram_html
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="admin_broadcast_confirm")

CB_SEND = "adm:bc:go"
CB_DROP = "adm:bc:no"
FLOW = "platform_broadcast"
HUB_CALLBACK = "nv:adm:broadcast"
REJECTED_BY_TELEGRAM = "⚠️ تلگرام این متن را نپذیرفت (احتمالاً لینک یا برچسب نادرست). متن را اصلاح و دوباره بفرستید."
ONLY_TEXT = "⚠️ فقط پیام متنی قابل ارسال گروهی است."
DROPPED = "ارسال لغو شد؛ پیامی فرستاده نشد."


class BroadcastConfirmStates(StatesGroup):
    confirm = State()


def broadcast_source_html(message: Message) -> str:
    """The owner's message as HTML: Telegram formatting when used, else the typed text (simple tags allowed)."""
    if message.entities:
        return message.html_text or ""
    return message.text or ""


def confirm_markup(total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ ارسال برای {total} نفر", callback_data=CB_SEND)],
        [InlineKeyboardButton(text="✖️ لغو ارسال", callback_data=CB_DROP)],
    ])


async def _back(session: AsyncSession) -> InlineKeyboardMarkup:
    return with_inline_back(None, await get_all_settings(session), HUB_CALLBACK)


async def preview_broadcast(message: Message, state: FSMContext, session: AsyncSession, *, audience: str) -> None:
    """Check the text, then ask for buttons; the preview and «ارسال» come after (``_preview``)."""
    back = await _back(session)
    if not message.text:
        await finish_text_step(message, state, text=ONLY_TEXT, inline=back)
        await state.clear()
        return
    try:
        body = normalize_telegram_html(broadcast_source_html(message).strip())
        await count_platform_targets(session, audience)
    except ValueError as exc:
        await finish_text_step(message, state, text=f"⚠️ {html.escape(user_safe_error(exc))}", inline=back)
        await state.clear()
        return
    await state.update_data(bc_text=body, bc_audience=audience)
    await ask_for_buttons(message, state, flow=FLOW, shop_id=None)


async def _preview(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, buttons: list[ButtonInput], **_: object
) -> None:
    """Send the message as customers will see it, buttons included, and ask to confirm; nothing is queued yet."""
    message = callback.message
    if not isinstance(message, Message):
        return
    data = await state.get_data()
    body, audience = str(data.get("bc_text") or ""), str(data.get("bc_audience") or "")
    back = await _back(session)
    try:
        stored = await broadcast_buttons(session, buttons, shop_id=None)
        total = await count_platform_targets(session, audience)
    except ValueError as exc:
        await state.clear()
        await message.answer(f"⚠️ {html.escape(user_safe_error(exc))}", reply_markup=back)
        return
    rows = preview_keyboard(stored)
    try:
        await message.answer(body, parse_mode="HTML", **({"reply_markup": InlineKeyboardMarkup(inline_keyboard=rows)} if rows else {}))
    except TelegramBadRequest:
        logger.info("broadcast preview rejected by Telegram", exc_info=True)
        await state.clear()
        await message.answer(REJECTED_BY_TELEGRAM, reply_markup=back)
        return
    await state.set_state(BroadcastConfirmStates.confirm)
    await state.update_data(bc_buttons=stored, bc_token=new_request_token())
    label = html.escape(AUDIENCE_LABELS.get(audience, audience))
    await message.answer(f"👆 پیش‌نمایش پیام بالا.\nبرای «{label}» ({total} نفر) فرستاده شود؟", reply_markup=confirm_markup(total))


register_flow(FLOW, _preview)


@router.callback_query(F.data == CB_SEND, BroadcastConfirmStates.confirm)
@require_bot_owner_handler
async def send_confirmed(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext) -> None:
    data = await state.get_data()
    back = await _back(session)
    try:
        log = await queue_broadcast(
            session,
            text=str(data.get("bc_text") or ""),
            audience=str(data.get("bc_audience") or ""),
            created_by=str(db_user.telegram_id),
            request=BroadcastRequest(data.get("bc_token"), data.get("bc_buttons")),
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer()
    rows = [[InlineKeyboardButton(text="📜 وضعیت این ارسال", callback_data=f"bch:v:{log.id}")], *back.inline_keyboard]
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ در صف ارسال قرار گرفت\nمخاطبان: {log.total}\nارسال به‌تدریج انجام می‌شود؛ پیشرفت را از «تاریخچهٔ ارسال‌ها» ببینید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.in_({CB_SEND, CB_DROP}))
async def drop_broadcast(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """«لغو», or «ارسال» on a card whose confirmation is no longer pending: nothing is sent."""
    if await state.get_state() == BroadcastConfirmStates.confirm.state:
        await state.clear()
    await callback.answer(feedback.CANCELLED if callback.data == CB_DROP else DROPPED)
    if callback.message:
        await safe_edit_text(callback.message, DROPPED, reply_markup=await _back(session))
