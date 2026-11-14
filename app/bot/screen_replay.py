"""Go back one screen by replaying the button that opens it.

A prompt records the callback data of the screen it was opened from (``return_to``). Cancel then
feeds that callback through the dispatcher as if the person had tapped it again, so every gate,
scope check and renderer of that screen runs exactly as on a real tap, and no screen needs a
second «reopen» implementation.

Telegram does not know these taps, so their ``answerCallbackQuery`` calls are dropped by
``install_replay_guard`` and the flood limiter does not count them. Inside ``watching_replay()``
the dropped answers and the messages the screen edited are noted, so the caller can tell whether
the screen opened and show the person an alert it raised (no access, not found) on the real tap.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageReplyMarkup,
    EditMessageText,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User

logger = logging.getLogger(__name__)

REPLAY_PREFIX = "replay:"
# Callback data is at most 64 bytes; anything longer did not come from one of our buttons.
_MAX_DATA = 64
_dispatcher: Dispatcher | None = None


@dataclass(frozen=True, slots=True)
class ReplayAnswer:
    """What a replayed screen answered to its synthetic tap."""

    text: str | None
    show_alert: bool


@dataclass(slots=True)
class ReplayLog:
    """What happened while it was watched: replayed answers and the messages the bot edited."""

    answers: list[ReplayAnswer] = field(default_factory=list)
    # (chat id, message id) of every message edited while watched.
    edited: set[tuple[int, int]] = field(default_factory=set)

    def was_edited(self, chat_id: int, message_id: int) -> bool:
        return (int(chat_id), int(message_id)) in self.edited

    def edited_in(self, chat_id: int) -> set[int]:
        return {message_id for chat, message_id in self.edited if chat == int(chat_id)}


# Watches nest (a replay inside an update that is itself watched); every open watch sees each request.
_replay_logs: ContextVar[tuple[ReplayLog, ...]] = ContextVar("replay_logs", default=())

_EDIT_METHODS = (EditMessageText, EditMessageCaption, EditMessageMedia, EditMessageReplyMarkup)


@contextmanager
def watching_replay() -> Iterator[ReplayLog]:
    log = ReplayLog()
    token = _replay_logs.set((*_replay_logs.get(), log))
    try:
        yield log
    finally:
        _replay_logs.reset(token)


def set_dispatcher(dispatcher: Dispatcher) -> None:
    global _dispatcher
    _dispatcher = dispatcher


async def _drop_replay_answers(
    make_request: NextRequestMiddlewareType[TelegramType], bot: Bot, method: TelegramMethod[TelegramType]
) -> Any:
    logs = _replay_logs.get()
    if isinstance(method, AnswerCallbackQuery) and method.callback_query_id.startswith(REPLAY_PREFIX):
        for log in logs:
            log.answers.append(ReplayAnswer(text=method.text, show_alert=bool(method.show_alert)))
        return True
    result = await make_request(bot, method)
    if logs and isinstance(method, _EDIT_METHODS) and method.message_id is not None and _is_int(method.chat_id):
        for log in logs:
            log.edited.add((int(method.chat_id), int(method.message_id)))
    return result


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def install_replay_guard(bot: Bot) -> Bot:
    bot.session.middleware(_drop_replay_answers)
    return bot


def is_replayed(event: Any) -> bool:
    callback = event.callback_query if isinstance(event, Update) else event
    return isinstance(callback, CallbackQuery) and callback.id.startswith(REPLAY_PREFIX)


async def replay_screen(
    bot: Bot,
    *,
    user: User,
    chat_id: int,
    data: str,
    message: Message | None = None,
    message_id: int | None = None,
) -> bool:
    """Open the screen behind ``data`` in ``message`` (or the bot message ``message_id``).

    Returns False when nothing handled it, so the caller can fall back.
    """
    if _dispatcher is None or not data or len(data.encode()) > _MAX_DATA:
        return False
    if message is None:
        if not message_id:
            return False
        message = Message(
            message_id=int(message_id),
            date=datetime.now(timezone.utc),
            chat=Chat(id=int(chat_id), type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="bot"),
        )
    callback = CallbackQuery(
        id=f"{REPLAY_PREFIX}{secrets.token_hex(8)}",
        from_user=user,
        chat_instance=str(chat_id),
        message=message,
        data=data,
    )
    try:
        result = await _dispatcher.feed_update(bot, Update(update_id=0, callback_query=callback))
    except Exception:  # the screen's own error handling already ran; fall back to the caller's
        logger.warning("screen replay failed data=%s", data, exc_info=True)
        return False
    return result is not UNHANDLED
