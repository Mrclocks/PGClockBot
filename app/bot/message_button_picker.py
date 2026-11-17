"""The bot step that adds buttons under a staff message: a question, then a checklist of one to four.

After the text, the bot asks «دکمه پایین پیام باشه؟». «ارسال بدون دکمه» continues at once; «بله» shows
the checklist (bot screens, and «تمدید سرویس» in a direct message) and the shop's saved sets. A saved
set (made on the web, where links and colours are set) is taken as it is. The flow that asked
continues through the finisher it registered, with the chosen buttons; the buttons are validated
there, against the sender's shop and permissions, before anything is sent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.nav_input import finish_text_step
from app.db.models import BotUser
from app.services.message_buttons import (
    BUTTONS_MAX,
    ButtonInput,
    list_presets,
    preset_inputs,
    target_options,
)

router = Router(name="message_button_picker")

PREFIX = "mbp:"
CB_YES = "mbp:y"
CB_NO = "mbp:n"
CB_DONE = "mbp:ok"
CB_TOGGLE = "mbp:t:"
CB_PRESET = "mbp:p:"
QUESTION = "دکمه پایین پیام باشه؟"
CHECKLIST = f"دکمه‌های زیر پیام را انتخاب کنید (۱ تا {BUTTONS_MAX})؛ قالب‌های ذخیره‌شده پایین فهرست‌اند."
TOO_MANY = f"حداکثر {BUTTONS_MAX} دکمه"
NONE_CHOSEN = "دست‌کم یک دکمه انتخاب کنید یا «بدون دکمه» را بزنید"
EXPIRED = "این انتخاب منقضی شده؛ دوباره پیام را بفرستید"

# finisher(callback, state, session, db_user, buttons, **middleware data)
Finisher = Callable[..., Awaitable[None]]
_FINISHERS: dict[str, Finisher] = {}


class PickerStates(StatesGroup):
    picking = State()


def register_flow(name: str, finisher: Finisher) -> None:
    """A flow that asks for buttons; ``finisher`` continues it with the chosen buttons."""
    _FINISHERS[name] = finisher


def question_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، دکمه بگذار", callback_data=CB_YES)],
        [InlineKeyboardButton(text="➡️ ارسال بدون دکمه", callback_data=CB_NO)],
    ])


def checklist_markup(selected: list[str], *, renew: bool, presets: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Two columns of ✅/⬜ targets, then one row per saved set, then «ادامه»."""
    options = [
        InlineKeyboardButton(text=f"{'✅' if key in selected else '⬜'} {label}", callback_data=f"{CB_TOGGLE}{key}")
        for key, label in target_options(renew=renew, links=False)
    ]
    rows = [options[i:i + 2] for i in range(0, len(options), 2)]
    rows += [[InlineKeyboardButton(text=f"📚 قالب: {name}", callback_data=f"{CB_PRESET}{pid}")] for pid, name in presets]
    rows.append([
        InlineKeyboardButton(text=f"ادامه ({len(selected)})", callback_data=CB_DONE),
        InlineKeyboardButton(text="بدون دکمه", callback_data=CB_NO),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def toggle(selected: list[str], key: str) -> list[str]:
    """``selected`` with ``key`` switched; ``ValueError`` past the limit. Order is the order tapped."""
    if key in selected:
        return [k for k in selected if k != key]
    if len(selected) >= BUTTONS_MAX:
        raise ValueError(TOO_MANY)
    return [*selected, key]


async def ask_for_buttons(
    message: Message, state: FSMContext, *, flow: str, shop_id: int | None, renew: bool = False
) -> None:
    """Ask the question in place of the text prompt (the same panel); the flow's own data stays in ``state``."""
    await finish_text_step(message, state, text=QUESTION, inline=question_markup())
    await state.set_state(PickerStates.picking)
    await state.update_data(mbp_flow=flow, mbp_shop=shop_id, mbp_renew=renew, mbp_selected=[])


async def _show_checklist(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    presets = [(int(p.id), p.name) for p in await list_presets(session, data.get("mbp_shop"))]
    markup = checklist_markup(list(data.get("mbp_selected") or []), renew=bool(data.get("mbp_renew")), presets=presets)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(CHECKLIST, reply_markup=markup)


async def _finish(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
                  buttons: list[ButtonInput], extra: dict) -> None:
    data = await state.get_data()
    finisher = _FINISHERS.get(str(data.get("mbp_flow") or ""))
    if finisher is None:
        await state.clear()
        await callback.answer(EXPIRED, show_alert=True)
        return
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    await message.edit_reply_markup(reply_markup=None)
    await finisher(callback, state, session, db_user, buttons, **extra)


def _extra(kwargs: dict) -> dict:
    return {k: kwargs[k] for k in ("is_reseller_bot", "reseller_owner_id") if k in kwargs}


@router.callback_query(F.data == CB_YES, PickerStates.picking)
async def on_yes(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _show_checklist(callback, state, session)


@router.callback_query(F.data.startswith(CB_TOGGLE), PickerStates.picking)
async def on_toggle(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    key = (callback.data or "")[len(CB_TOGGLE):]
    allowed = {k for k, _l in target_options(renew=bool(data.get("mbp_renew")), links=False)}
    if key not in allowed:
        await callback.answer(EXPIRED, show_alert=True)
        return
    try:
        selected = toggle(list(data.get("mbp_selected") or []), key)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(mbp_selected=selected)
    await callback.answer()
    await _show_checklist(callback, state, session)


@router.callback_query(F.data == CB_DONE, PickerStates.picking)
async def on_done(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, **kwargs) -> None:
    selected = list((await state.get_data()).get("mbp_selected") or [])
    if not selected:
        await callback.answer(NONE_CHOSEN, show_alert=True)
        return
    await _finish(callback, state, session, db_user, [ButtonInput(key) for key in selected], _extra(kwargs))


@router.callback_query(F.data == CB_NO, PickerStates.picking)
async def on_no(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, **kwargs) -> None:
    await _finish(callback, state, session, db_user, [], _extra(kwargs))


@router.callback_query(F.data.regexp(r"^mbp:p:\d{1,10}$"), PickerStates.picking)
async def on_preset(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, **kwargs) -> None:
    data = await state.get_data()
    # The preset is read with the shop the flow started in, never one named in the callback.
    buttons = await preset_inputs(session, data.get("mbp_shop"), int((callback.data or "")[len(CB_PRESET):]))
    if not buttons:
        await callback.answer(EXPIRED, show_alert=True)
        return
    await _finish(callback, state, session, db_user, buttons, _extra(kwargs))


@router.callback_query(F.data.startswith(PREFIX))
async def on_stale(callback: CallbackQuery) -> None:
    """A tap on an old question or checklist after the flow ended."""
    await callback.answer(EXPIRED, show_alert=True)
