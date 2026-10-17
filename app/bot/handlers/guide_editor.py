"""«آموزش اتصال» editor in the bot: title, text, photos and videos, links, audience, order.

``owner_router`` serves the platform owner on the main bot and is registered behind the
owner-principal middleware; ``shop_router`` serves a shop owner for their own shop only. The
rules live in ``app.services.connection_guides``; these handlers parse, check the actor, call
it and render.

Callback data: ``gde:<a|r>:<view>[:<guide id>[:<arg>…]]``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
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
from app.bot.handlers.guides import guide_detail_keyboard, send_guide_album
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
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.connection_guides import (
    add_guide_link,
    add_guide_media,
    create_guide,
    delete_guide,
    find_guide,
    get_connection_guides,
    move_guide,
    move_guide_media,
    remove_guide_link,
    remove_guide_media,
    update_guide,
)
from app.services.formatting import format_message
from app.services.guide_media import (
    KIND_PHOTO,
    KIND_VIDEO,
    MAX_MEDIA_PER_GUIDE,
    GuideMediaError,
    load_file_id_cache,
    media_to_send,
)

logger = logging.getLogger(__name__)
owner_router = Router(name="guide_editor_owner")
shop_router = Router(name="guide_editor_shop")

ROOT = "gde"
# Settings section (same id in the owner and shop settings) that links here.
GUIDES_SECTION = "guides"
BODY_MAX = 4000
PREVIEW_CHARS = 300
_SETTINGS_BACK = {"a": "adm:st:sec:guides", "r": "res:st:sec:guides"}
_CANCEL_CODES = {"a": "gde_a", "r": "gde_r"}
_TAGS = re.compile(r"<[^>]+>")
_STATE_KEY = "gde_target"
# Album messages arrive together; saving them one at a time per scope keeps every file.
_scope_locks: dict[str, asyncio.Lock] = {}

BTN_ENTRY = "📘 ویرایش آموزش‌ها"
BTN_NEW = "➕ آموزش تازه"
BTN_DONE = "✅ تمام"
TITLE_LIST = "📘 <b>آموزش‌های اتصال</b>\nیک آموزش را برای ویرایش انتخاب کنید."
EMPTY_LIST = "📘 <b>آموزش‌های اتصال</b>\nهنوز آموزشی ندارید."
ASK_TITLE = "تیتر آموزش را بفرستید (همان متنی که روی دکمه می‌آید، حداکثر ۶۴ نویسه)."
ASK_BODY = "متن آموزش را بفرستید. بولد، لینک و ایموجی پرمیوم حفظ می‌شود."
ASK_MEDIA = (
    "عکس یا ویدیو بفرستید؛ چند فایل را می‌توانید یکجا (آلبوم) یا پشت سر هم بفرستید.\n"
    "حداکثر {max} فایل. آخر کار «{done}» را بزنید."
)
MEDIA_PROGRESS = "✅ {count} فایل در این آموزش هست. باز هم بفرستید یا «{done}» را بزنید."
ASK_LINK_URL = "لینک ویدیو را بفرستید (https از youtube.com، youtu.be یا t.me)."
ASK_LINK_LABEL = "متن دکمهٔ این لینک را بفرستید، یا برای «▶️ دیدن ویدیو» علامت - بفرستید."
NOT_MEDIA = "فقط عکس یا ویدیو بفرستید، یا «{done}» را بزنید."
TEXT_REQUIRED = "متن را خالی نگذارید."
BODY_TOO_LONG = "متن آموزش حداکثر ۴۰۰۰ نویسه است."
DELETE_CONFIRM = "⚠️ آموزش «{title}» با همهٔ فایل‌ها و لینک‌هایش حذف شود؟ این کار برگشت ندارد."


class OwnerGuideInput(StatesGroup):
    title = State()
    body = State()
    new_title = State()
    new_body = State()
    media = State()
    link_url = State()
    link_label = State()


class ShopGuideInput(StatesGroup):
    title = State()
    body = State()
    new_title = State()
    new_body = State()
    media = State()
    link_url = State()
    link_label = State()


@dataclass(frozen=True, slots=True)
class Actor:
    """Who edits: the platform (``reseller_id`` None) or one shop."""

    audience: str
    reseller_id: int | None

    @property
    def lock(self) -> asyncio.Lock:
        return _scope_locks.setdefault(f"{self.audience}:{self.reseller_id}", asyncio.Lock())

    def data(self, view: str, *args: object) -> str:
        return ":".join([ROOT, self.audience, view, *(str(a) for a in args)])


def entry_button(*, for_reseller: bool) -> InlineKeyboardButton:
    """Button placed in the «آموزش اتصال» settings section."""
    return InlineKeyboardButton(text=BTN_ENTRY, callback_data=f"{ROOT}:{'r' if for_reseller else 'a'}:l")


def _plain_preview(body: str) -> str:
    text = html.unescape(_TAGS.sub("", body or "")).strip()
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS].rstrip() + "…"
    return html.escape(text) or "—"


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def list_view(actor: Actor, guides: list[dict[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_button(f"{'' if g.get('enabled', True) else '⏸ '}{g.get('title') or 'آموزش'}"[:64], actor.data("g", g["id"]))]
        for g in guides[: kb.LIST_BUTTON_CAP]
    ]
    rows.append([_button(BTN_NEW, actor.data("n"))])
    rows.append([kb.back_button(None, _SETTINGS_BACK[actor.audience])])
    return (TITLE_LIST if guides else EMPTY_LIST), InlineKeyboardMarkup(inline_keyboard=rows)


def card_view(actor: Actor, guide: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    gid = guide["id"]
    enabled = guide.get("enabled", True)
    audience = "نمایندگان" if guide.get("audience") == "reseller" else "کاربران"
    media, links = guide.get("media") or [], guide.get("links") or []
    text = (
        f"📘 <b>{html.escape(str(guide.get('title') or 'آموزش'))}</b>\n"
        f"مخاطب: {audience} · وضعیت: {'روشن ✅' if enabled else 'خاموش ⏸'}\n"
        f"عکس و ویدیو: {len(media)} · لینک: {len(links)}\n\n"
        f"{_plain_preview(str(guide.get('body') or ''))}"
    )
    rows = [
        [_button("👁 پیش‌نمایش برای مشتری", actor.data("p", gid))],
        [_button("✏️ تیتر", actor.data("t", gid)), _button("✏️ متن", actor.data("b", gid))],
        [_button(f"🖼 عکس و ویدیو ({len(media)})", actor.data("m", gid)), _button(f"🔗 لینک‌ها ({len(links)})", actor.data("L", gid))],
        [_button(f"👥 مخاطب: {audience}", actor.data("u", gid)), _button("⏸ خاموش کن" if enabled else "▶️ روشن کن", actor.data("e", gid))],
        [
            _button(f"{'✅' if guide.get('deep_link') else '⬜️'} دکمهٔ لینک اشتراک", actor.data("k", gid)),
        ],
        [_button("⬆️ بالاتر", actor.data("o", gid, "u")), _button("⬇️ پایین‌تر", actor.data("o", gid, "d"))],
        [_button("🗑 حذف آموزش", actor.data("x", gid))],
        [kb.back_button(None, actor.data("l"))],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def media_view(actor: Actor, guide: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    gid = guide["id"]
    media = guide.get("media") or []
    rows = []
    for index, item in enumerate(media):
        label = f"{'🎬 ویدیو' if item['kind'] == KIND_VIDEO else '🖼 عکس'} {index + 1}"
        rows.append([
            _button(label, actor.data("g", gid)),
            _button("▲", actor.data("mo", gid, item["id"], "u")),
            _button("▼", actor.data("mo", gid, item["id"], "d")),
            _button("🗑", actor.data("md", gid, item["id"])),
        ])
    if len(media) < MAX_MEDIA_PER_GUIDE:
        rows.append([_button("➕ افزودن عکس یا ویدیو", actor.data("ma", gid))])
    rows.append([kb.back_button(None, actor.data("g", gid))])
    text = "🖼 <b>عکس و ویدیوی آموزش</b>\n" + (
        "ترتیب همین فهرست، ترتیب آلبوم است." if media else "هنوز فایلی اضافه نشده است."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def links_view(actor: Actor, guide: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    gid = guide["id"]
    links = guide.get("links") or []
    rows = [[_button(f"🗑 {link['label']}"[:64], actor.data("Ld", gid, index))] for index, link in enumerate(links)]
    if len(links) < 3:
        rows.append([_button("➕ افزودن لینک", actor.data("La", gid))])
    rows.append([kb.back_button(None, actor.data("g", gid))])
    lines = "\n".join(f"• {html.escape(link['label'])}: <code>{html.escape(link['url'])}</code>" for link in links)
    text = "🔗 <b>لینک‌های آموزش</b>\n" + (lines + "\nبرای حذف، روی لینک بزنید." if links else "هنوز لینکی اضافه نشده است.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_view(actor: Actor, guide: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_button("🗑 بله، حذف شود", actor.data("X", guide["id"]))],
        [kb.back_button(None, actor.data("g", guide["id"]))],
    ]
    title = html.escape(str(guide.get("title") or "آموزش"))
    return DELETE_CONFIRM.format(title=title), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    if callback.message:
        await safe_edit_text(callback.message, view[0], reply_markup=view[1])


async def _guide(session: AsyncSession, actor: Actor, gid: str) -> dict[str, Any]:
    return await find_guide(session, gid, reseller_id=actor.reseller_id)


async def _toggle(session: AsyncSession, actor: Actor, gid: str, view: str) -> dict[str, Any]:
    guide = await _guide(session, actor, gid)
    if view == "e":
        changes = {"enabled": not guide.get("enabled", True)}
    elif view == "u":
        changes = {"audience": "user" if guide.get("audience") == "reseller" else "reseller"}
    else:
        changes = {"deep_link": not guide.get("deep_link")}
    return await update_guide(session, gid, reseller_id=actor.reseller_id, **changes)


async def _preview(callback: CallbackQuery, session: AsyncSession, actor: Actor, gid: str) -> None:
    guide = await _guide(session, actor, gid)
    cache = await load_file_id_cache(session, reseller_id=actor.reseller_id)
    album = media_to_send(guide, bot_id=callback.bot.id, cache=cache, reseller_id=actor.reseller_id)
    chat_id = callback.message.chat.id
    if album:
        await send_guide_album(callback.bot, chat_id, album)
    text = format_message(f"📘 {guide.get('title') or 'آموزش'}", (guide.get("body") or "").strip() or "—")
    await callback.bot.send_message(chat_id, text, reply_markup=guide_detail_keyboard(guide, {}, svc_id=0))
    card_text, card_markup = card_view(actor, guide)
    await callback.bot.send_message(chat_id, card_text, reply_markup=card_markup)


ViewHandler = Callable[[CallbackQuery, AsyncSession, Actor, list[str]], Awaitable[None]]


async def _view_list(callback: CallbackQuery, session: AsyncSession, actor: Actor, _args: list[str]) -> None:
    await _show(callback, list_view(actor, await get_connection_guides(session, reseller_id=actor.reseller_id)))


async def _view_card(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _show(callback, card_view(actor, await _guide(session, actor, args[0])))


async def _view_toggle(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str], view: str) -> None:
    await _show(callback, card_view(actor, await _toggle(session, actor, args[0], view)))


async def _view_move(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await move_guide(session, args[0], step=-1 if args[1] == "u" else 1, reseller_id=actor.reseller_id)
    await _show(callback, card_view(actor, await _guide(session, actor, args[0])))


async def _view_media(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _show(callback, media_view(actor, await _guide(session, actor, args[0])))


async def _view_media_move(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    async with actor.lock:
        await move_guide_media(session, args[0], args[1], step=-1 if args[2] == "u" else 1, reseller_id=actor.reseller_id)
    await _show(callback, media_view(actor, await _guide(session, actor, args[0])))


async def _view_media_delete(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    async with actor.lock:
        await remove_guide_media(session, args[0], args[1], reseller_id=actor.reseller_id)
    await _show(callback, media_view(actor, await _guide(session, actor, args[0])))


async def _view_links(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _show(callback, links_view(actor, await _guide(session, actor, args[0])))


async def _view_link_delete(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    guide = await remove_guide_link(session, args[0], int(args[1]), reseller_id=actor.reseller_id)
    await _show(callback, links_view(actor, guide))


async def _view_delete_ask(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _show(callback, _confirm_view(actor, await _guide(session, actor, args[0])))


async def _view_delete(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    async with actor.lock:
        await delete_guide(session, args[0], reseller_id=actor.reseller_id)
    await _view_list(callback, session, actor, [])


async def _view_preview(callback: CallbackQuery, session: AsyncSession, actor: Actor, args: list[str]) -> None:
    await _preview(callback, session, actor, args[0])


_VIEWS: dict[str, tuple[int, ViewHandler]] = {
    "l": (0, _view_list),
    "g": (1, _view_card),
    "o": (2, _view_move),
    "m": (1, _view_media),
    "mo": (3, _view_media_move),
    "md": (2, _view_media_delete),
    "L": (1, _view_links),
    "Ld": (2, _view_link_delete),
    "x": (1, _view_delete_ask),
    "X": (1, _view_delete),
    "p": (1, _view_preview),
}
_TOGGLES = frozenset({"e", "u", "k"})


def _input_states(actor: Actor) -> type[StatesGroup]:
    return OwnerGuideInput if actor.audience == "a" else ShopGuideInput


async def _ask(
    callback: CallbackQuery, state: FSMContext, actor: Actor, *, prompt: str, fsm_state: State, gid: str | None,
    extra: InlineKeyboardMarkup | None = None,
) -> None:
    await state.update_data(**{_STATE_KEY: {"audience": actor.audience, "guide_id": gid}})
    await ask_text(
        callback, state, prompt=prompt, cancel_code=_CANCEL_CODES[actor.audience], fsm_state=fsm_state,
        extra_inline=extra, edit=True,
    )


async def _start_input(callback: CallbackQuery, state: FSMContext, actor: Actor, view: str, args: list[str]) -> bool:
    """Prompts that wait for typed text or files; returns False when ``view`` is not one."""
    states = _input_states(actor)
    gid = args[0] if args else None
    if view == "n":
        await _ask(callback, state, actor, prompt=ASK_TITLE, fsm_state=states.new_title, gid=None)
    elif view == "t" and gid:
        await _ask(callback, state, actor, prompt=ASK_TITLE, fsm_state=states.title, gid=gid)
    elif view == "b" and gid:
        await _ask(callback, state, actor, prompt=ASK_BODY, fsm_state=states.body, gid=gid)
    elif view == "ma" and gid:
        done = InlineKeyboardMarkup(inline_keyboard=[[_button(BTN_DONE, actor.data("f", gid))]])
        prompt = ASK_MEDIA.format(max=MAX_MEDIA_PER_GUIDE, done=BTN_DONE)
        await _ask(callback, state, actor, prompt=prompt, fsm_state=states.media, gid=gid, extra=done)
    elif view == "La" and gid:
        await _ask(callback, state, actor, prompt=ASK_LINK_URL, fsm_state=states.link_url, gid=gid)
    else:
        return False
    return True


async def handle_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    parts = (callback.data or "").split(":")
    view, args = (parts[2], parts[3:]) if len(parts) >= 3 else ("", [])
    try:
        if view == "f":
            await state.set_state(None)
            await callback.answer()
            await _view_card(callback, session, actor, args)
            return
        if await _start_input(callback, state, actor, view, args):
            await callback.answer()
            return
        if view in _TOGGLES and len(args) == 1:
            await _view_toggle(callback, session, actor, args, view)
            await callback.answer()
            return
        arity, handler = _VIEWS.get(view, (-1, None))
        if handler is None or len(args) != arity:
            await callback.answer(feedback.INVALID, show_alert=True)
            return
        await handler(callback, session, actor, args)
        await callback.answer()
    except (GuideMediaError, ValueError) as exc:
        await callback.answer(str(exc) if isinstance(exc, GuideMediaError) else feedback.INVALID, show_alert=True)


async def _target(state: FSMContext) -> dict[str, Any]:
    return dict((await state.get_data()).get(_STATE_KEY) or {})


async def _finish(message: Message, state: FSMContext, view: tuple[str, InlineKeyboardMarkup]) -> None:
    await state.set_state(None)
    await delete_user_message(message)
    await finish_text_step(message, state, text=view[0], inline=view[1])


async def _save_text_input(message: Message, session: AsyncSession, state: FSMContext, actor: Actor, step: str) -> None:
    target = await _target(state)
    gid = target.get("guide_id")
    if step in {"title", "new_title"}:
        value = (message.text or "").strip()
    else:
        value = (message.html_text or "").strip() if message.text else ""
    if not value:
        await message.answer(TEXT_REQUIRED)
        return
    if step in {"body", "new_body"} and len(value) > BODY_MAX:
        await message.answer(BODY_TOO_LONG)
        return
    states = _input_states(actor)
    if step == "new_title":
        await state.update_data(**{_STATE_KEY: {**target, "title": value}})
        await _next_prompt(message, state, actor, prompt=ASK_BODY, fsm_state=states.new_body)
        return
    if step == "new_body":
        guide = await create_guide(session, title=target.get("title") or "", body=value, reseller_id=actor.reseller_id)
        await _finish(message, state, card_view(actor, guide))
        return
    field = "title" if step == "title" else "body"
    guide = await update_guide(session, gid, reseller_id=actor.reseller_id, **{field: value})
    await _finish(message, state, card_view(actor, guide))


async def _next_prompt(message: Message, state: FSMContext, actor: Actor, *, prompt: str, fsm_state: State) -> None:
    """Second step of a two-step input: turn the same prompt bubble into the next question."""
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
            logger.debug("guide editor prompt edit failed; sending a new one", exc_info=True)
    await ask_text(message, state, prompt=prompt, cancel_code=_CANCEL_CODES[actor.audience], fsm_state=fsm_state)


async def _save_link_input(message: Message, session: AsyncSession, state: FSMContext, actor: Actor, step: str) -> None:
    target = await _target(state)
    text = (message.text or "").strip()
    if not text:
        await message.answer(TEXT_REQUIRED)
        return
    if step == "link_url":
        await state.update_data(**{_STATE_KEY: {**target, "url": text}})
        await _next_prompt(message, state, actor, prompt=ASK_LINK_LABEL, fsm_state=_input_states(actor).link_label)
        return
    label = "" if text == "-" else text
    guide = await add_guide_link(session, target.get("guide_id") or "", label=label, url=target.get("url") or "",
                                 reseller_id=actor.reseller_id)
    await _finish(message, state, links_view(actor, guide))


async def _save_media_input(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    target = await _target(state)
    if message.photo:
        kind, file_id = KIND_PHOTO, message.photo[-1].file_id
    elif message.video:
        kind, file_id = KIND_VIDEO, message.video.file_id
    else:
        await message.answer(NOT_MEDIA.format(done=BTN_DONE))
        return
    async with actor.lock:
        media = await add_guide_media(
            session, target.get("guide_id") or "", kind=kind, bot_file_id=(message.bot.id, file_id),
            reseller_id=actor.reseller_id,
        )
    await _update_media_prompt(message, state, actor, target.get("guide_id") or "", len(media))


async def _update_media_prompt(message: Message, state: FSMContext, actor: Actor, gid: str, count: int) -> None:
    data = await state.get_data()
    prompt_id, chat_id = data.get(PROMPT_MSG_ID), data.get(PROMPT_CHAT_ID)
    if not prompt_id or not chat_id:
        return
    done = InlineKeyboardMarkup(inline_keyboard=[[_button(BTN_DONE, actor.data("f", gid))]])
    try:
        await message.bot.edit_message_text(
            chat_id=int(chat_id), message_id=int(prompt_id),
            text=MEDIA_PROGRESS.format(count=count, done=BTN_DONE), reply_markup=done,
        )
    except TelegramAPIError:
        # An unchanged or deleted prompt; the files are already saved.
        logger.debug("guide media prompt update failed", exc_info=True)


async def handle_message(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    current = (await state.get_state() or "").rsplit(":", 1)[-1]
    if kb.is_cancel_text(message.text):
        await _cancel(message, session, state, actor)
        return
    try:
        if current == "media":
            await _save_media_input(message, session, state, actor)
        elif current in {"link_url", "link_label"}:
            await _save_link_input(message, session, state, actor, current)
        else:
            await _save_text_input(message, session, state, actor, current)
    except GuideMediaError as exc:
        await message.answer(str(exc))


async def _view_after_cancel(session: AsyncSession, state: FSMContext, actor: Actor) -> tuple[str, InlineKeyboardMarkup]:
    """Back to the guide being edited, or to the list when it is new or gone; nothing is saved."""
    gid = (await _target(state)).get("guide_id")
    await state.set_state(None)
    if gid:
        try:
            return card_view(actor, await _guide(session, actor, gid))
        except GuideMediaError:
            pass
    return list_view(actor, await get_connection_guides(session, reseller_id=actor.reseller_id))


async def _cancel(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    await delete_user_message(message)
    text, markup = await _view_after_cancel(session, state, actor)
    await finish_text_step(message, state, text=text, inline=markup)


# --- Owner (platform guides, main bot) --------------------------------------------------------
_OWNER = Actor(audience="a", reseller_id=None)


@owner_router.callback_query(F.data.startswith("gde:a:"))
async def owner_guides(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    # The owner-principal middleware on this router already admitted the actor.
    await handle_callback(callback, session, state, _OWNER)


@owner_router.message(OwnerGuideInput.title)
@owner_router.message(OwnerGuideInput.body)
@owner_router.message(OwnerGuideInput.new_title)
@owner_router.message(OwnerGuideInput.new_body)
@owner_router.message(OwnerGuideInput.media)
@owner_router.message(OwnerGuideInput.link_url)
@owner_router.message(OwnerGuideInput.link_label)
async def owner_guide_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await handle_message(message, session, state, _OWNER)


# --- Shop (each shop's own guides) ------------------------------------------------------------
async def _shop_actor(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> Actor | None:
    shop_id = await shop_settings_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return Actor(audience="r", reseller_id=shop_id) if shop_id else None


@shop_router.callback_query(F.data.startswith("gde:r:"))
async def shop_guides(
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


@shop_router.message(ShopGuideInput.title)
@shop_router.message(ShopGuideInput.body)
@shop_router.message(ShopGuideInput.new_title)
@shop_router.message(ShopGuideInput.new_body)
@shop_router.message(ShopGuideInput.media)
@shop_router.message(ShopGuideInput.link_url)
@shop_router.message(ShopGuideInput.link_label)
async def shop_guide_input(
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


async def _reopen_owner(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext, **_kw: Any) -> None:
    await _cancel_into_panel(message, session, state, _OWNER)


async def _reopen_shop(
    message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext,
    *, is_reseller_bot: bool = False, reseller_owner_id: int | None = None, **_kw: Any,
) -> None:
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is not None:
        await _cancel_into_panel(message, session, state, actor)


async def _cancel_into_panel(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    """Inline «انصراف»: turn the prompt back into the guide card or the list."""
    text, markup = await _view_after_cancel(session, state, actor)
    await safe_edit_text(message, text, reply_markup=markup)


register_cancel_code(_CANCEL_CODES["a"], CancelEntry(reopen=_reopen_owner))
register_cancel_code(_CANCEL_CODES["r"], CancelEntry(reopen=_reopen_shop))
