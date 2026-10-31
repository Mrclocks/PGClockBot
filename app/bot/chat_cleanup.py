"""The lists behind a tidy chat: which messages are temporary, which one carries the keyboard.

``app.bot.chat_screen`` decides what each message is; this module remembers the temporary ones
and the menu-keyboard messages per (bot, chat) and deletes them when the screen moves on. A
panel that turns into something worth keeping, such as a payment confirmation, is taken off
the list with ``keep_message``.

The lists live in memory for speed and are mirrored to the database
(``app.services.chat_message_ledger``); after a restart a chat's list is read back on its first
cleanup, so messages sent before the restart are still deleted (within Telegram's 48-hour
delete window).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, ReplyKeyboardMarkup
from sqlalchemy.exc import SQLAlchemyError

from app.services.chat_message_ledger import KIND_HOME, KIND_THROWAWAY, StoredMessage, load_chat, writer

logger = logging.getLogger(__name__)

TRACKED_PER_CHAT = 30
# Homes are replaced as soon as a new one is sent, so only a few are ever outstanding.
TRACKED_HOMES_PER_CHAT = 5
TRACKED_CHATS = 20_000
# Telegram refuses to delete a message older than 48 hours; stay clear of the edge.
DELETE_WINDOW_SECONDS = 47 * 3600
# deleteMessages takes at most 100 ids per call.
DELETE_BATCH = 100

_cleanup_on: ContextVar[bool] = ContextVar("chat_cleanup_on", default=False)

ChatKey = tuple[int, int]


class ThrowawayLog:
    """Throwaway message ids per (bot, chat), oldest chats evicted first."""

    def __init__(self, *, per_chat: int = TRACKED_PER_CHAT, chats: int = TRACKED_CHATS) -> None:
        self._per_chat = per_chat
        self._chats = chats
        self._by_chat: OrderedDict[ChatKey, dict[int, float]] = OrderedDict()

    def add(self, key: ChatKey, message_id: int, now: float) -> None:
        ids = self._by_chat.setdefault(key, {})
        self._by_chat.move_to_end(key)
        ids[int(message_id)] = now
        while len(ids) > self._per_chat:
            del ids[min(ids)]
        while len(self._by_chat) > self._chats:
            self._by_chat.popitem(last=False)

    def forget(self, key: ChatKey, message_id: int) -> None:
        ids = self._by_chat.get(key)
        if ids is not None:
            ids.pop(int(message_id), None)

    def take_except(self, key: ChatKey, keep: int | Collection[int] | None, now: float) -> list[int]:
        """Remove and return the ids still young enough to delete, except those in ``keep``."""
        ids = self._by_chat.get(key)
        if not ids:
            return []
        keep_ids = {keep} if isinstance(keep, int) else set(keep or ())
        kept = {mid: at for mid, at in ids.items() if mid in keep_ids}
        fresh = sorted(mid for mid, at in ids.items() if mid not in keep_ids and now - at < DELETE_WINDOW_SECONDS)
        self._by_chat[key] = kept
        return fresh


_log = ThrowawayLog()
_homes = ThrowawayLog(per_chat=TRACKED_HOMES_PER_CHAT)
# Chats whose stored list was already read back since this process started.
_loaded: OrderedDict[ChatKey, None] = OrderedDict()


def _monotonic_at(sent_at: datetime, now_mono: float, now_utc: datetime) -> float:
    return now_mono - (now_utc - sent_at).total_seconds()


def _restore(key: ChatKey, rows: list[StoredMessage]) -> None:
    now_mono, now_utc = time.monotonic(), datetime.now(timezone.utc)
    for row in reversed(rows):  # oldest first, so the per-chat bound keeps the newest
        target = _homes if row.kind == KIND_HOME else _log
        target.add(key, row.message_id, _monotonic_at(row.sent_at, now_mono, now_utc))


async def _ensure_loaded(key: ChatKey) -> None:
    """Read the chat's stored list back once per process; a failed read only skips old messages."""
    if key in _loaded:
        return
    _loaded[key] = None
    while len(_loaded) > TRACKED_CHATS:
        _loaded.popitem(last=False)
    try:
        async with writer.sessions() as session:
            rows = await load_chat(session, key)
    except SQLAlchemyError:
        logger.warning("chat cleanup could not read the stored list chat=%s", key[1], exc_info=True)
        return
    _restore(key, rows)


def _take(log: ThrowawayLog, key: ChatKey, keep: int | Collection[int] | None, now: float) -> list[int]:
    ids = log.take_except(key, keep, now)
    writer.remove(key, ids)
    return ids


def is_cleanup_on() -> bool:
    return _cleanup_on.get()


@contextmanager
def cleanup_switch(on: bool) -> Iterator[None]:
    """Whether this update may delete messages (the bot's «پاک‌سازی خودکار» setting)."""
    token = _cleanup_on.set(on)
    try:
        yield
    finally:
        _cleanup_on.reset(token)


def chat_key(message: Message, bot: Bot | None = None) -> ChatKey | None:
    """(bot id, chat id) of a private chat; None for groups and channels."""
    chat = getattr(message, "chat", None)
    bot = bot or getattr(message, "bot", None)
    if getattr(chat, "type", None) != "private" or bot is None:
        return None
    try:
        return int(bot.id), int(chat.id)
    except (TypeError, ValueError):
        return None


def note_throwaway(message: Message | None, *, reply_markup: Any = None, bot: Bot | None = None) -> None:
    """Remember a panel or a menu tap so the next panel can delete it."""
    if message is None or not is_cleanup_on() or isinstance(reply_markup, ReplyKeyboardMarkup):
        return
    key = chat_key(message, bot)
    if key is not None and getattr(message, "message_id", None) is not None:
        note_id(key, int(message.message_id))


def note_id(key: ChatKey, message_id: int) -> None:
    """Mark a message temporary; nothing is noted while this bot's cleanup is off."""
    if not is_cleanup_on():
        return
    _log.add(key, message_id, time.monotonic())
    writer.add(key, message_id, KIND_THROWAWAY)


def keep_message(message: Message | None) -> None:
    """A panel now shows something the person should keep; never delete it."""
    key = chat_key(message) if message is not None else None
    if key is not None and getattr(message, "message_id", None) is not None:
        forget_id(key, int(message.message_id))


def forget_id(key: ChatKey, message_id: int) -> None:
    _log.forget(key, message_id)
    writer.remove(key, [message_id])


async def sweep_above(message: Message | None, *, panel_id: int | None = None) -> None:
    """Delete the throwaway messages in this chat, except the panel now on screen.

    The panel is ``message`` itself unless ``panel_id`` names another message in the chat.
    """
    if message is None or not is_cleanup_on():
        return
    key = chat_key(message)
    if key is None:
        return
    keep = panel_id if panel_id is not None else getattr(message, "message_id", None)
    await _ensure_loaded(key)
    stale = _take(_log, key, keep, time.monotonic())
    await delete_messages(message.bot, key[1], stale)


async def replace_carrier(sent: Message, *, track: bool = True) -> None:
    """``sent`` now carries the menu keyboard: delete the older keyboard messages.

    Only ``app.bot.chat_screen`` calls this, for a message that really carried the keyboard;
    the newest one is never deleted, so the menu always stays (iOS drops it otherwise).
    ``track=False`` is for a record carrying the keyboard (a delivery): it replaces the older
    keyboard messages but is never deleted itself.
    """
    if not is_cleanup_on():
        return
    key = chat_key(sent)
    if key is None:
        return
    await _ensure_loaded(key)
    now = time.monotonic()
    stale = [mid for mid in _take(_homes, key, int(sent.message_id), now) if mid != int(sent.message_id)]
    if track:
        _homes.add(key, int(sent.message_id), now)
        writer.add(key, int(sent.message_id), KIND_HOME)
    await delete_messages(sent.bot, key[1], sorted(stale))


async def replace_home(home: Message | None, *, sweep: bool = False) -> None:
    """A home screen was just shown: with ``sweep`` delete every temporary message but it.

    Which message carries the keyboard is tracked by ``chat_screen`` from what was really sent.
    """
    if home is None or not sweep or not is_cleanup_on():
        return
    key = chat_key(home)
    if key is None or getattr(home, "message_id", None) is None:
        return
    await _ensure_loaded(key)
    stale = _take(_log, key, int(home.message_id), time.monotonic())
    await delete_messages(home.bot, key[1], stale)


async def sweep_except(bot: Bot, key: ChatKey, keep: Collection[int]) -> None:
    """Delete the chat's temporary messages except ``keep`` (what the current update showed)."""
    if not is_cleanup_on():
        return
    await _ensure_loaded(key)
    stale = _take(_log, key, keep, time.monotonic())
    await delete_messages(bot, key[1], stale)


async def delete_messages(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    for start in range(0, len(message_ids), DELETE_BATCH):
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=message_ids[start : start + DELETE_BATCH])
        except TelegramAPIError:
            # Already gone or too old: the chat is merely less tidy.
            logger.debug("chat cleanup could not delete %s messages", len(message_ids), exc_info=True)
