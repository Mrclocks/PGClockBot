"""What stays on screen in a private chat, decided once for every message the bot sends.

The person sees one bottom keyboard, one live panel and the messages worth keeping:

- **Keyboard.** Only a main menu keyboard (``keyboard_guard``) is ever sent. Sending the same
  keyboard the chat already shows is pointless, so the keyboard is dropped from that message;
  a home that must re-attach it (``/start``, «شروع مجدد», leaving the customer preview) runs in
  ``fresh_keyboard()``. When a new keyboard message is sent the older ones are deleted, so
  exactly one message carries the menu.
- **Temporary by default.** Every other message the bot sends or edits in the chat of the
  current update, and every text the person types there, is temporary. When an update shows
  something, the temporary messages from earlier updates are deleted.
- **Records stay.** Deliveries, configs, credentials, payment results and backups are sent
  inside ``chat_records.keep_sent()``; ``chat_cleanup.keep_message`` keeps a panel that turned
  into one (a payment confirmation).

Messages sent to other chats (staff notices, a customer's delivery approved by staff) are left
alone. The «پاک‌سازی خودکار صفحهٔ ربات» switch turns deleting off; the keyboard rule stays.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)

from app.bot import chat_cleanup
from app.bot.keyboard_guard import asks_for_contact, is_main_keyboard, keyboard_fingerprint
from app.services.chat_cleanup_config import is_chat_cleanup_on
from app.services.chat_records import is_keeping
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

SEND_METHODS = (SendMessage, SendPhoto, SendDocument, SendVideo, SendAnimation, SendAudio, SendVoice)
EDIT_METHODS = (EditMessageText, EditMessageCaption, EditMessageMedia)
# Chats whose shown keyboard this process remembers; older ones simply get it sent again.
REMEMBERED_CHATS = 20_000

ChatKey = tuple[int, int]


@dataclass(slots=True)
class Turn:
    """One update in one private chat and the bot messages it showed."""

    key: ChatKey
    shown: set[int] = field(default_factory=set)


_turn: ContextVar[Turn | None] = ContextVar("chat_screen_turn", default=None)
_fresh: ContextVar[bool] = ContextVar("chat_screen_fresh_keyboard", default=False)
# The keyboard each chat shows now, as a fingerprint.
_shown_keyboard: OrderedDict[ChatKey, str] = OrderedDict()


def current_turn() -> Turn | None:
    return _turn.get()


@contextmanager
def turn(key: ChatKey | None) -> Iterator[Turn | None]:
    """Track the bot's messages in ``key`` for the length of one update (None: not tracked)."""
    token = _turn.set(Turn(key) if key is not None else None)
    try:
        yield _turn.get()
    finally:
        _turn.reset(token)


@contextmanager
def fresh_keyboard() -> Iterator[None]:
    """The menu keyboard sent inside this block is attached even if the chat already shows it."""
    token = _fresh.set(True)
    try:
        yield
    finally:
        _fresh.reset(token)


def private_chat_key(bot: Bot, chat_id: Any) -> ChatKey | None:
    """(bot id, chat id) when ``chat_id`` is a person's private chat (positive id)."""
    try:
        chat = int(chat_id)
        return (int(bot.id), chat) if chat > 0 else None
    except (TypeError, ValueError):
        return None


def _remember_keyboard(key: ChatKey, fingerprint: str | None) -> None:
    if fingerprint is None:
        _shown_keyboard.pop(key, None)
        return
    _shown_keyboard[key] = fingerprint
    _shown_keyboard.move_to_end(key)
    while len(_shown_keyboard) > REMEMBERED_CHATS:
        _shown_keyboard.popitem(last=False)


def shows_keyboard(bot: Bot, chat_id: Any, markup: Any) -> bool:
    """True when this chat already shows ``markup`` (as far as this process knows)."""
    key = private_chat_key(bot, chat_id)
    if key is None or not isinstance(markup, ReplyKeyboardMarkup) or _fresh.get():
        return False
    return _shown_keyboard.get(key) == keyboard_fingerprint(markup)


def keyboard_plan(key: ChatKey | None, markup: Any) -> tuple[bool, str | None]:
    """(keep the keyboard on this message, its fingerprint when it becomes the chat's keyboard)."""
    if key is None or not isinstance(markup, ReplyKeyboardMarkup):
        return True, None
    fingerprint = keyboard_fingerprint(markup)
    if asks_for_contact(markup):
        return True, fingerprint
    if not is_main_keyboard(markup):
        logger.warning("dropped a non-menu reply keyboard chat=%s first=%r", key[1], fingerprint[:60])
        return False, None
    if not _fresh.get() and _shown_keyboard.get(key) == fingerprint:
        return False, None
    return True, fingerprint


async def screen_requests(make_request: NextRequestMiddlewareType[Any], bot: Bot, method: TelegramMethod[Any]) -> Any:
    """Bot session middleware applying the rules above to every request."""
    if not isinstance(method, SEND_METHODS + EDIT_METHODS):
        return await make_request(bot, method)
    key = private_chat_key(bot, getattr(method, "chat_id", None))
    new_keyboard: str | None = None
    if isinstance(method, SEND_METHODS):
        keep_keyboard, new_keyboard = keyboard_plan(key, method.reply_markup)
        if not keep_keyboard:
            method = method.model_copy(update={"reply_markup": None})
        elif key is not None and isinstance(method.reply_markup, ReplyKeyboardRemove):
            _remember_keyboard(key, None)
    result = await make_request(bot, method)
    if key is None:
        return result
    if isinstance(method, SEND_METHODS) and isinstance(result, Message):
        await _after_send(key, result, new_keyboard)
    elif isinstance(method, EDIT_METHODS):
        _after_edit(key, method)
    return result


async def _after_send(key: ChatKey, sent: Message, new_keyboard: str | None) -> None:
    active = _turn.get()
    if active is not None and active.key == key:
        active.shown.add(int(sent.message_id))
    if new_keyboard is not None:
        _remember_keyboard(key, new_keyboard)
        # The new keyboard message replaces the older ones; a delivery carrying it stays itself.
        await chat_cleanup.replace_carrier(sent, track=not is_keeping())
        return
    if active is not None and active.key == key and not is_keeping():
        chat_cleanup.note_id(key, int(sent.message_id))


def _after_edit(key: ChatKey, method: EditMessageText | EditMessageCaption | EditMessageMedia) -> None:
    """An edited message is on screen now, so the end of this update keeps it.

    Whether it is temporary does not change: a panel stays temporary, a notification that
    arrived on its own (a payment review, a ticket) stays until its own flow retires it.
    """
    active = _turn.get()
    if active is None or active.key != key or method.inline_message_id or method.message_id is None:
        return
    active.shown.add(int(method.message_id))


async def end_turn(bot: Bot, active: Turn | None) -> None:
    """The update showed something: delete the temporary messages of earlier updates."""
    if active is None or not active.shown or not chat_cleanup.is_cleanup_on():
        return
    await chat_cleanup.sweep_except(bot, active.key, active.shown)


def install_screen_rules(bot: Bot) -> Bot:
    bot.session.middleware(screen_requests)
    return bot


def _update_key(bot: Bot | None, event: TelegramObject) -> tuple[ChatKey | None, Message | None]:
    """(the private chat of this update, the person's message when they sent one)."""
    if bot is None:
        return None, None
    message = getattr(event, "message", None)
    if isinstance(message, Message):
        chat = message.chat if message.chat.type == "private" else None
        return (private_chat_key(bot, chat.id) if chat else None), message
    callback = getattr(event, "callback_query", None)
    tapped = callback.message if isinstance(callback, CallbackQuery) else None
    if isinstance(tapped, Message) and tapped.chat.type == "private":
        return private_chat_key(bot, tapped.chat.id), None
    return None, None


class ChatScreenMiddleware(BaseMiddleware):
    """Per update: read this bot's cleanup switch, note what the person typed, then tidy up."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        bot: Bot | None = data.get("bot")
        if session is None:
            return await handler(event, data)
        cleanup_on = is_chat_cleanup_on(await get_all_settings(session))
        key, message = _update_key(bot, event) if cleanup_on else (None, None)
        with chat_cleanup.cleanup_switch(cleanup_on), turn(key) as active:
            if key is not None and message is not None and message.text:
                # Typed text and menu taps are temporary; a ticket's text is kept by its handler.
                chat_cleanup.note_id(key, int(message.message_id))
            result = await handler(event, data)
            if bot is not None:
                await end_turn(bot, active)
            return result
