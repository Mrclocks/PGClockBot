"""«سوالات متداول» for customers: the question buttons, one answer, and «جوابم را نگرفتم».

The shop is the bot the update arrived on (``reseller_owner_id``; ``None`` on the platform bot), never
a value from the callback. Every id in a callback is looked up inside that shop by
``app.services.faq``, so a forged id from another shop just reads as «not found».

Callback data: ``faq:l`` (list), ``faq:v:<id>`` (answer), ``faq:t:<id>`` (open a ticket about it).
``help:faq`` is the older entry used by message buttons and links; it opens the same list and
falls back to the old single FAQ text while a shop has not made any question yet.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.handlers.support import SupportStates
from app.bot.nav_input import ask_text
from app.bot.tg_utils import safe_edit_text
from app.services import bot_feedback_texts as feedback
from app.services.button_icons import ICON_KEY
from app.services.button_styles import style_or_none
from app.services.faq import NOT_FOUND, FaqButton, faq_buttons, record_faq_ticket, record_faq_view
from app.services.rich_text import outbound_setting_text, rich_plain_text
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="faq")

LIST_CALLBACK = "faq:l"
LEGACY_CALLBACK = "help:faq"
SUPPORT_HOME_CALLBACK = "nv:s:home"
ITEM_STYLE_ID = "faq_item"
LIST_TITLE = "❓ <b>سوالات متداول</b>\nسوال خود را انتخاب کنید؛ اگر جوابتان را نگرفتید از زیر جواب، تیکت بفرستید."
EMPTY_LIST = "❓ <b>سوالات متداول</b>\nهنوز سوالی ثبت نشده است. از بخش پشتیبانی برای ما پیام بفرستید."
BTN_NOT_ANSWERED = "🙋 جوابم را نگرفتم — تیکت"
BTN_ALL_QUESTIONS = "❓ همهٔ سوال‌ها"
ASK_TICKET_BODY = "❓ <b>{question}</b>\n\nمشکل خود را بنویسید؛ همین سوال موضوع تیکت می‌شود."
TICKET_SUBJECT_MAX = 120


def item_style(ui: dict | None, stored: str | None) -> str | None:
    """The button color: its own, or the «هر سوال متداول» color when it inherits."""
    if stored is None:
        return style_or_none(ui, ITEM_STYLE_ID, fallback="primary")
    return stored or None


def question_button(ui: dict | None, item: FaqButton) -> InlineKeyboardButton:
    extra = {ICON_KEY: item.icon_custom_emoji_id} if item.icon_custom_emoji_id else {}
    return InlineKeyboardButton(
        text=item.label, callback_data=f"faq:v:{item.item_id}", style=item_style(ui, item.button_style), **extra
    )


def list_keyboard(ui: dict | None, items: list[FaqButton]) -> InlineKeyboardMarkup:
    rows = [[question_button(ui, item)] for item in items]
    rows.append([kb.back_button(ui, SUPPORT_HOME_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def answer_keyboard(ui: dict | None, item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_NOT_ANSWERED, callback_data=f"faq:t:{item_id}")],
            [InlineKeyboardButton(text=BTN_ALL_QUESTIONS, callback_data=LIST_CALLBACK)],
            [kb.back_button(ui, SUPPORT_HOME_CALLBACK)],
        ]
    )


def answer_text(question: str, answer: str) -> str:
    """Question as a bold title over the stored answer (already Telegram-safe HTML)."""
    return f"❓ <b>{html.escape(question)}</b>\n\n{answer}"


def parse_item_id(data: str | None) -> int | None:
    """The numeric id at the end of ``faq:<v|t>:<id>``; ``None`` for anything else."""
    tail = (data or "").rsplit(":", 1)[-1]
    if not tail.isdigit() or len(tail) > 10:
        return None
    return int(tail)


async def _show_list(callback: CallbackQuery, session: AsyncSession, reseller_id: int | None, *, legacy: bool) -> None:
    ui = await get_all_settings(session)
    items = await faq_buttons(session, reseller_id=reseller_id)
    if items or not legacy:
        await safe_edit_text(callback.message, LIST_TITLE if items else EMPTY_LIST, reply_markup=list_keyboard(ui, items))
        return
    # A shop that has not made questions yet keeps showing its old single FAQ message.
    raw = ui.get("faq_text") or ""
    if not rich_plain_text(raw).strip():
        await safe_edit_text(callback.message, EMPTY_LIST, reply_markup=kb.back_home(ui))
        return
    text, send_kw = outbound_setting_text(raw, title="❓ سوالات متداول")
    await safe_edit_text(callback.message, text, reply_markup=kb.back_home(ui), **send_kw)


@router.callback_query(F.data.in_({LIST_CALLBACK, LEGACY_CALLBACK}))
async def faq_list(
    callback: CallbackQuery, session: AsyncSession, reseller_owner_id: int | None = None
) -> None:
    await callback.answer()
    if callback.message:
        await _show_list(callback, session, reseller_owner_id, legacy=callback.data == LEGACY_CALLBACK)


@router.callback_query(F.data.startswith("faq:v:"))
async def faq_answer(
    callback: CallbackQuery, session: AsyncSession, reseller_owner_id: int | None = None
) -> None:
    item_id = parse_item_id(callback.data)
    item = await record_faq_view(session, item_id, reseller_id=reseller_owner_id) if item_id else None
    if item is None:
        await callback.answer(NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    ui = await get_all_settings(session)
    await safe_edit_text(callback.message, answer_text(item.question, item.answer), reply_markup=answer_keyboard(ui, item.id))


@router.callback_query(F.data.startswith("faq:t:"))
async def faq_ticket(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    reseller_owner_id: int | None = None,
) -> None:
    item_id = parse_item_id(callback.data)
    item = await record_faq_ticket(session, item_id, reseller_id=reseller_owner_id) if item_id else None
    if item is None:
        await callback.answer(NOT_FOUND if item_id else feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    subject = f"❓ {item.question}"[:TICKET_SUBJECT_MAX]
    await state.update_data(subject=subject)
    await ask_text(
        callback,
        state,
        prompt=ASK_TICKET_BODY.format(question=html.escape(item.question)),
        cancel_code="s_body",
        fsm_state=SupportStates.body,
        edit=True,
        return_to=LIST_CALLBACK,
    )
