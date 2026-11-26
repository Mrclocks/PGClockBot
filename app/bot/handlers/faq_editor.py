"""«سوالات متداول» editor in the bot: question, answer, button color, premium icon, visibility, order.

``owner_router`` serves the platform owner on the main bot and is registered behind the
owner-principal middleware; ``shop_router`` serves a shop owner (``shop_settings`` permission) for
their own shop only. The rules live in ``app.services.faq``; these handlers parse, resolve the
actor, call it and render. A premium emoji sent with the question becomes the button's icon.

Callback data: ``fqe:<a|r>:<view>[:<question id>[:<arg>]]``.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.handlers.button_colors import shop_settings_shop_id
from app.bot.handlers.faq import answer_text, item_style
from app.bot.nav_input import (
    PROMPT_CHAT_ID,
    PROMPT_MSG_ID,
    CancelEntry,
    ask_text,
    cancel_keyboard,
    delete_user_message,
    finish_text_step,
    register_cancel_code,
)
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, FaqItem
from app.services import bot_feedback_texts as feedback
from app.services.button_icons import ICON_KEY, PREMIUM_ICON_HINT, split_label_and_icon
from app.services.faq import (
    MAX_FAQ_ITEMS,
    FaqDraft,
    FaqError,
    button_label,
    change_faq_item,
    create_faq_item,
    delete_faq_item,
    get_faq_item,
    list_faq_items,
    move_faq_item,
)
from app.services.numbers import to_fa_digits
from app.services.rich_text import rich_plain_text
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
owner_router = Router(name="faq_editor_owner")
shop_router = Router(name="faq_editor_shop")

ROOT = "fqe"
_SETTINGS_BACK = {"a": "adm:st:sec:support", "r": "res:st:sec:support"}
_CANCEL_CODES = {"a": "fqe_a", "r": "fqe_r"}
_STATE_KEY = "fqe_target"
PREVIEW_CHARS = 300

BTN_ENTRY = "❓ سوالات متداول"
BTN_NEW = "➕ سوال تازه"
TITLE_LIST = "❓ <b>سوالات متداول</b>\n{count} سوال · هر سوال یک دکمه در «پشتیبانی» است. یکی را برای ویرایش باز کنید."
EMPTY_LIST = "❓ <b>سوالات متداول</b>\nهنوز سوالی نساخته‌اید. با «سوال تازه» اولین را بسازید."
ASK_QUESTION = "متن سوال را بفرستید (همان که روی دکمه می‌آید، یک خط).\n" + PREMIUM_ICON_HINT
ASK_ANSWER = "جواب سوال را بفرستید. بولد، لینک و اموجی پریمیوم حفظ می‌شود."
TEXT_REQUIRED = "متن را خالی نگذارید."
DELETE_CONFIRM = "⚠️ سوال «{question}» حذف شود؟ آمار بازدیدش هم پاک می‌شود و برگشت ندارد."

# (code in callback, stored button_style, label)
COLOR_CHOICES: tuple[tuple[str, str | None, str], ...] = (
    ("i", None, "رنگ پیش‌فرض سوال‌ها"),
    ("w", "", "⚪️ سفید"),
    ("p", "primary", "🔵 آبی"),
    ("s", "success", "🟢 سبز"),
    ("d", "danger", "🔴 قرمز"),
)
_COLOR_BY_CODE = {code: style for code, style, _label in COLOR_CHOICES}
_COLOR_LABEL = {style: label for _code, style, label in COLOR_CHOICES}


class OwnerFaqInput(StatesGroup):
    question = State()
    answer = State()
    new_question = State()
    new_answer = State()


class ShopFaqInput(StatesGroup):
    question = State()
    answer = State()
    new_question = State()
    new_answer = State()


@dataclass(frozen=True, slots=True)
class Actor:
    """Who edits: the platform (``reseller_id`` None) or one shop."""

    audience: str
    reseller_id: int | None

    def data(self, view: str, *args: object) -> str:
        return ":".join([ROOT, self.audience, view, *(str(a) for a in args)])


def entry_button(*, for_reseller: bool) -> InlineKeyboardButton:
    """Button placed in the «پشتیبان‌ها» settings section."""
    return InlineKeyboardButton(text=BTN_ENTRY, callback_data=f"{ROOT}:{'r' if for_reseller else 'a'}:l")


def _button(text: str, data: str, **extra: Any) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=data, **extra)


def _icon(item: FaqItem) -> dict[str, str]:
    return {ICON_KEY: item.icon_custom_emoji_id} if item.icon_custom_emoji_id else {}


def _preview(answer: str) -> str:
    text = rich_plain_text(answer).strip()
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS].rstrip() + "…"
    return html.escape(text) or "—"


def list_view(actor: Actor, items: list[FaqItem]) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_button(f"{'' if item.is_active else '⏸ '}{button_label(item.question)}", actor.data("g", item.id), **_icon(item))]
        for item in items
    ]
    if len(items) < MAX_FAQ_ITEMS:
        rows.append([_button(BTN_NEW, actor.data("n"))])
    rows.append([kb.back_button(None, _SETTINGS_BACK[actor.audience])])
    text = TITLE_LIST.format(count=to_fa_digits(len(items))) if items else EMPTY_LIST
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def card_view(actor: Actor, item: FaqItem) -> tuple[str, InlineKeyboardMarkup]:
    iid = item.id
    text = (
        f"❓ <b>{html.escape(item.question)}</b>\n"
        f"وضعیت: {'روشن ✅' if item.is_active else 'خاموش ⏸'} · رنگ: {_COLOR_LABEL.get(item.button_style, '—')}"
        f" · اموجی پریمیوم: {'دارد' if item.icon_custom_emoji_id else 'ندارد'}\n"
        f"بازدید: {to_fa_digits(item.view_count)} · «جوابم را نگرفتم»: {to_fa_digits(item.ticket_count)}\n\n"
        f"{_preview(item.answer)}"
    )
    rows = [
        [_button("👁 پیش‌نمایش برای مشتری", actor.data("p", iid))],
        [_button("✏️ سوال", actor.data("q", iid)), _button("✏️ جواب", actor.data("b", iid))],
        [_button("🎨 رنگ دکمه", actor.data("c", iid)), _button("⏸ خاموش کن" if item.is_active else "▶️ روشن کن", actor.data("e", iid))],
        [_button("⬆️ بالاتر", actor.data("o", iid, "u")), _button("⬇️ پایین‌تر", actor.data("o", iid, "d"))],
        [_button("🗑 حذف سوال", actor.data("x", iid))],
        [kb.back_button(None, actor.data("l"))],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def color_view(actor: Actor, item: FaqItem) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_button(f"{'✅ ' if item.button_style == style else ''}{label}", actor.data("s", item.id, code))]
        for code, style, label in COLOR_CHOICES
    ]
    rows.append([kb.back_button(None, actor.data("g", item.id))])
    text = f"🎨 <b>رنگ دکمهٔ «{html.escape(button_label(item.question))}»</b>\nرنگ پیش‌فرض همهٔ سوال‌ها از «رنگ دکمه‌ها» عوض می‌شود."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def preview_view(actor: Actor, item: FaqItem, ui: dict) -> tuple[str, InlineKeyboardMarkup]:
    """The answer as the customer sees it, under the question's own button (tap returns to the card)."""
    button = _button(button_label(item.question), actor.data("g", item.id), style=item_style(ui, item.button_style), **_icon(item))
    rows = [[button], [kb.back_button(None, actor.data("g", item.id))]]
    return answer_text(item.question, item.answer), InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_view(actor: Actor, item: FaqItem) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_button("🗑 بله، حذف شود", actor.data("X", item.id))],
        [kb.back_button(None, actor.data("g", item.id))],
    ]
    return DELETE_CONFIRM.format(question=html.escape(button_label(item.question))), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    if callback.message:
        await safe_edit_text(callback.message, view[0], reply_markup=view[1])


def _item_id(args: list[str]) -> int:
    if not args or not args[0].isdigit() or len(args[0]) > 10:
        raise ValueError("bad question id")
    return int(args[0])


async def _item(session: AsyncSession, actor: Actor, args: list[str]) -> FaqItem:
    return await get_faq_item(session, _item_id(args), reseller_id=actor.reseller_id)


ViewHandler = Callable[[CallbackQuery, AsyncSession, Actor, list[str]], Awaitable[None]]


async def _view_list(callback: CallbackQuery, session: AsyncSession, actor: Actor, _args: list[str]) -> None:
    await _show(callback, list_view(actor, await list_faq_items(session, reseller_id=actor.reseller_id)))


async def _view_card(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _show(callback, card_view(actor, await _item(session, actor, args)))


async def _view_toggle(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    item = await _item(session, actor, args)
    item = await change_faq_item(session, item.id, reseller_id=actor.reseller_id, is_active=not item.is_active)
    await _show(callback, card_view(actor, item))


async def _view_move(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    item_id = _item_id(args)
    await move_faq_item(session, item_id, step=-1 if args[1] == "u" else 1, reseller_id=actor.reseller_id)
    await _show(callback, card_view(actor, await get_faq_item(session, item_id, reseller_id=actor.reseller_id)))


async def _view_colors(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _show(callback, color_view(actor, await _item(session, actor, args)))


async def _view_set_color(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    if args[1] not in _COLOR_BY_CODE:
        raise ValueError("bad color")
    item = await change_faq_item(session, _item_id(args), reseller_id=actor.reseller_id, button_style=_COLOR_BY_CODE[args[1]])
    await _show(callback, card_view(actor, item))


async def _view_preview(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    ui = await get_all_settings(session, reseller_id=actor.reseller_id)
    await _show(callback, preview_view(actor, await _item(session, actor, args), ui))


async def _view_delete_ask(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _show(callback, _confirm_view(actor, await _item(session, actor, args)))


async def _view_delete(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await delete_faq_item(session, _item_id(args), reseller_id=actor.reseller_id)
    await _view_list(callback, session, actor, [])


_VIEWS: dict[str, tuple[int, ViewHandler]] = {
    "l": (0, _view_list),
    "g": (1, _view_card),
    "e": (1, _view_toggle),
    "o": (2, _view_move),
    "c": (1, _view_colors),
    "s": (2, _view_set_color),
    "p": (1, _view_preview),
    "x": (1, _view_delete_ask),
    "X": (1, _view_delete),
}


def _input_states(actor: Actor) -> type[StatesGroup]:
    return OwnerFaqInput if actor.audience == "a" else ShopFaqInput


async def _start_input(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, actor: Actor, view: str, args: list[str]
) -> bool:
    """Prompts that wait for typed text; returns False when ``view`` is not one."""
    states = _input_states(actor)
    if view == "n":
        prompt, fsm_state, item_id = ASK_QUESTION, states.new_question, None
    elif view in {"q", "b"}:
        item_id = (await _item(session, actor, args)).id
        prompt, fsm_state = (ASK_QUESTION, states.question) if view == "q" else (ASK_ANSWER, states.answer)
    else:
        return False
    await state.update_data(**{_STATE_KEY: {"audience": actor.audience, "item_id": item_id}})
    await ask_text(callback, state, prompt=prompt, cancel_code=_CANCEL_CODES[actor.audience], fsm_state=fsm_state, edit=True)
    return True


async def handle_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    parts = (callback.data or "").split(":")
    view, args = (parts[2], parts[3:]) if len(parts) >= 3 else ("", [])
    try:
        if await _start_input(callback, session, state, actor, view, args):
            await callback.answer()
            return
        arity, handler = _VIEWS.get(view, (-1, None))
        if handler is None or len(args) != arity:
            await callback.answer(feedback.INVALID, show_alert=True)
            return
        await handler(callback, session, actor, args)
        await callback.answer()
    except FaqError as exc:
        await callback.answer(str(exc), show_alert=True)
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)


async def _target(state: FSMContext) -> dict[str, Any]:
    return dict((await state.get_data()).get(_STATE_KEY) or {})


async def _finish(message: Message, state: FSMContext, view: tuple[str, InlineKeyboardMarkup]) -> None:
    await state.set_state(None)
    await delete_user_message(message)
    await finish_text_step(message, state, text=view[0], inline=view[1])


async def _next_prompt(message: Message, state: FSMContext, actor: Actor, *, prompt: str, fsm_state: State) -> None:
    """Second step of a new question: turn the same prompt bubble into the answer question."""
    await delete_user_message(message)
    await state.set_state(fsm_state)
    data = await state.get_data()
    prompt_id, chat_id = data.get(PROMPT_MSG_ID), data.get(PROMPT_CHAT_ID)
    if prompt_id and chat_id and int(chat_id) == int(message.chat.id):
        try:
            await message.bot.edit_message_text(
                chat_id=int(chat_id), message_id=int(prompt_id), text=prompt,
                reply_markup=cancel_keyboard(_CANCEL_CODES[actor.audience]),
            )
            return
        except TelegramAPIError:
            logger.debug("faq editor prompt edit failed; sending a new one", exc_info=True)
    await ask_text(message, state, prompt=prompt, cancel_code=_CANCEL_CODES[actor.audience], fsm_state=fsm_state)


async def _save_question(message: Message, session: AsyncSession, state: FSMContext, actor: Actor, step: str) -> None:
    """A premium emoji in the question becomes the button icon; a question without one clears it."""
    target = await _target(state)
    question, icon = split_label_and_icon(message.text, message.entities)
    if not question:
        await message.answer(TEXT_REQUIRED)
        return
    if step == "new_question":
        await state.update_data(**{_STATE_KEY: {**target, "question": question, ICON_KEY: icon}})
        await _next_prompt(message, state, actor, prompt=ASK_ANSWER, fsm_state=_input_states(actor).new_answer)
        return
    item = await change_faq_item(
        session, int(target.get("item_id") or 0), reseller_id=actor.reseller_id, question=question, icon_custom_emoji_id=icon
    )
    await _finish(message, state, card_view(actor, item))


async def _save_answer(message: Message, session: AsyncSession, state: FSMContext, actor: Actor, step: str) -> None:
    target = await _target(state)
    answer = (message.html_text or "").strip() if message.text else ""
    if not answer:
        await message.answer(TEXT_REQUIRED)
        return
    if step == "new_answer":
        draft = FaqDraft(question=str(target.get("question") or ""), answer=answer, icon_custom_emoji_id=target.get(ICON_KEY))
        item = await create_faq_item(session, draft, reseller_id=actor.reseller_id)
    else:
        item = await change_faq_item(session, int(target.get("item_id") or 0), reseller_id=actor.reseller_id, answer=answer)
    await _finish(message, state, card_view(actor, item))


async def handle_message(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    step = (await state.get_state() or "").rsplit(":", 1)[-1]
    if kb.is_cancel_text(message.text):
        await _cancel(message, session, state, actor)
        return
    try:
        if step in {"question", "new_question"}:
            await _save_question(message, session, state, actor, step)
        else:
            await _save_answer(message, session, state, actor, step)
    except FaqError as exc:
        # The prompt stays open so staff can send a corrected text.
        await message.answer(html.escape(str(exc)))


async def _view_after_cancel(session: AsyncSession, state: FSMContext, actor: Actor) -> tuple[str, InlineKeyboardMarkup]:
    """Back to the question being edited, or to the list when it is new or gone; nothing is saved."""
    item_id = (await _target(state)).get("item_id")
    await state.set_state(None)
    if item_id:
        try:
            return card_view(actor, await get_faq_item(session, int(item_id), reseller_id=actor.reseller_id))
        except FaqError:
            logger.debug("faq editor: question %s gone before cancel", item_id)
    return list_view(actor, await list_faq_items(session, reseller_id=actor.reseller_id))


async def _cancel(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    await delete_user_message(message)
    text, markup = await _view_after_cancel(session, state, actor)
    await finish_text_step(message, state, text=text, inline=markup)


# --- Owner (platform questions, main bot) -------------------------------------------------------
_OWNER = Actor(audience="a", reseller_id=None)


@owner_router.callback_query(F.data.startswith("fqe:a:"))
async def owner_faq(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    # The owner-principal middleware on this router already admitted the actor.
    await handle_callback(callback, session, state, _OWNER)


@owner_router.message(OwnerFaqInput.question)
@owner_router.message(OwnerFaqInput.answer)
@owner_router.message(OwnerFaqInput.new_question)
@owner_router.message(OwnerFaqInput.new_answer)
async def owner_faq_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await handle_message(message, session, state, _OWNER)


# --- Shop (each shop's own questions) -----------------------------------------------------------
async def _shop_actor(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> Actor | None:
    shop_id = await shop_settings_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return Actor(audience="r", reseller_id=shop_id) if shop_id else None


@shop_router.callback_query(F.data.startswith("fqe:r:"))
async def shop_faq(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.NO_SHOP_SETTINGS_ACCESS, show_alert=True)
        return
    await handle_callback(callback, session, state, actor)


@shop_router.message(ShopFaqInput.question)
@shop_router.message(ShopFaqInput.answer)
@shop_router.message(ShopFaqInput.new_question)
@shop_router.message(ShopFaqInput.new_answer)
async def shop_faq_input(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await state.set_state(None)
        await message.answer(feedback.NO_SHOP_SETTINGS_ACCESS)
        return
    await handle_message(message, session, state, actor)


async def _cancel_into_panel(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    """Inline «انصراف»: turn the prompt back into the question card or the list."""
    text, markup = await _view_after_cancel(session, state, actor)
    await safe_edit_text(message, text, reply_markup=markup)


async def _reopen_owner(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext, **_kw: Any) -> None:
    await _cancel_into_panel(message, session, state, _OWNER)


async def _reopen_shop(
    message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext,
    *, is_reseller_bot: bool = False, reseller_owner_id: int | None = None, **_kw: Any,
) -> None:
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is not None:
        await _cancel_into_panel(message, session, state, actor)


register_cancel_code(_CANCEL_CODES["a"], CancelEntry(reopen=_reopen_owner))
register_cancel_code(_CANCEL_CODES["r"], CancelEntry(reopen=_reopen_shop))
