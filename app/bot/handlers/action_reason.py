"""Ask the acting staff member for a reason before a destructive Telegram admin action.

A handler calls ``take_reason``. Without a stored reason for that exact callback it gets ``None``,
and ``ask_reason`` has already put the prompt on screen and moved the chat into the reason state.
The typed reason is stored in the person's own FSM data, bound to the callback it was asked for, and
the bot shows a button carrying that same callback. Pressing it runs the handler again, which now
finds and consumes the reason, so one reason can never authorize a different action.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.tg_utils import safe_edit_text
from app.services.action_reasons import REASON_MIN, ReasonRequired, clean_reason

router = Router(name="action_reason")

KEY_PENDING = "arsn_pending"
KEY_LABEL = "arsn_label"
KEY_DONE_FOR = "arsn_done_for"
KEY_REASON = "arsn_reason"
CANCEL_CALLBACK = "arsn:x"
CONTINUE_LABEL = "✅ ادامه"
CANCEL_LABEL = "انصراف"
CANCELLED_TEXT = "لغو شد. هیچ تغییری انجام نشد."
NO_STATE_TEXT = "ثبت علت ممکن نشد. دوباره از منو شروع کنید."


class ActionReasonStates(StatesGroup):
    reason = State()


def _cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=CANCEL_LABEL, callback_data=CANCEL_CALLBACK)]])


async def take_reason(state: FSMContext | None, callback: CallbackQuery) -> str | None:
    """The reason collected for exactly this callback (consumed), or ``None`` when none exists."""
    if state is None:
        return None
    data = await state.get_data()
    reason = data.get(KEY_REASON)
    if not reason or data.get(KEY_DONE_FOR) != callback.data:
        return None
    await state.update_data(**{KEY_REASON: None, KEY_DONE_FOR: None})
    return str(reason)


async def ask_reason(callback: CallbackQuery, state: FSMContext | None, *, label: str) -> None:
    """Put the reason prompt on screen for ``callback`` and wait for the typed reason."""
    if state is None:
        await callback.answer(NO_STATE_TEXT, show_alert=True)
        return
    await state.update_data(**{KEY_PENDING: callback.data, KEY_LABEL: label, KEY_REASON: None, KEY_DONE_FOR: None})
    await state.set_state(ActionReasonStates.reason)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✍️ {label} را بنویسید (حداقل {REASON_MIN} نویسه).",
            reply_markup=_cancel_markup(),
        )


@router.message(ActionReasonStates.reason, F.text)
async def reason_typed(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get(KEY_PENDING)
    if not pending:
        await state.set_state(None)
        return
    try:
        reason = clean_reason(message.text)
    except ReasonRequired as exc:
        await message.answer(str(exc), reply_markup=_cancel_markup())
        return
    await state.update_data(**{KEY_REASON: reason, KEY_DONE_FOR: pending, KEY_PENDING: None})
    await state.set_state(None)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=CONTINUE_LABEL, callback_data=str(pending))]])
    await message.answer("علت ثبت شد. برای انجام عملیات ادامه را بزنید.", reply_markup=markup)


@router.callback_query(F.data == CANCEL_CALLBACK)
async def reason_cancelled(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(**{KEY_PENDING: None, KEY_REASON: None, KEY_DONE_FOR: None})
    await state.set_state(None)
    await callback.answer(CANCELLED_TEXT)
    if callback.message:
        await safe_edit_text(callback.message, CANCELLED_TEXT)
