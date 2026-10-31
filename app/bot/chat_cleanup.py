"""Keep the private chat tidy: a new panel deletes the throwaway messages above it.

Only messages noted here as throwaway are ever deleted: panels, prompts and fixed one-line
notices the bot sends, and the person's menu-button taps and typed form replies. Everything else stays: deliveries, configs,
receipts, ticket replies, staff notices, and the message carrying the menu keyboard
(deleting that one drops the menu on iOS). A panel that turns into something worth
keeping, such as a payment confirmation, is taken off the list with ``keep_message``.

Home messages (the welcome and «منوی اصلی» carrying the main menu keyboard) are the one
exception to the keyboard rule: a new home replaces the older ones, because the new one
now carries the keyboard. Only the newest home is ever left on screen.

The list lives in memory for speed and is mirrored to the database
(``app.services.chat_message_ledger``); after a restart a chat's list is read back on its first
cleanup, so welcomes and panels sent before the restart are still deleted (within Telegram's
48-hour delete window).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Message, ReplyKeyboardMarkup, TelegramObject
from sqlalchemy.exc import SQLAlchemyError

from app.bot.reply_keyboard_state import is_menu_tap
from app.services import bot_feedback_texts as feedback
from app.services.chat_cleanup_config import is_chat_cleanup_on
from app.services.chat_message_ledger import KIND_HOME, KIND_THROWAWAY, StoredMessage, load_chat, writer
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

TRACKED_PER_CHAT = 30
# Homes are replaced as soon as a new one is sent, so only a few are ever outstanding.
TRACKED_HOMES_PER_CHAT = 5
TRACKED_CHATS = 20_000
# Telegram refuses to delete a message older than 48 hours; stay clear of the edge.
DELETE_WINDOW_SECONDS = 47 * 3600
# deleteMessages takes at most 100 ids per call.
DELETE_BATCH = 100

# Fixed one-line replies that only acknowledge a step; none of them records anything.
SHORT_NOTICES = frozenset(
    {
        "⏳",
        feedback.CANCELLED,
        feedback.CANCELLED_SHORT,
        feedback.CANCELLED_BACK_TO_HUB,
        feedback.SAVED,
        feedback.SAVED_CHECK,
        feedback.SAVED_WITH_ICON,
        feedback.COLOR_SAVED,
        feedback.UPDATED,
        feedback.DELETED,
        feedback.CONNECTION_DELETED,
        feedback.MAIN_KEYBOARD_RESTORED,
        feedback.INVALID,
        feedback.NOT_FOUND,
        feedback.ENTER_VALID_NUMBER,
        feedback.ENTER_VALID_NUMBER_SHORT,
        feedback.ENTER_ONE_VALID_NUMBER,
        feedback.NAME_REQUIRED,
        feedback.PICK_SECTION_BELOW,
        feedback.PICK_OPTION_BELOW,
        feedback.PICK_CATEGORY_BELOW,
        feedback.NOT_ADMIN_REPLY,
        feedback.NO_ACCESS_REPLY,
        feedback.ACTION_NOT_ALLOWED_REPLY,
        feedback.NO_SHOP_SETTINGS_ACCESS,
        feedback.CATEGORY_NOT_FOUND,
        feedback.CATEGORY_INVALID,
        feedback.TICKET_ALREADY_CLOSED,
        feedback.NO_PENDING_RECEIPT,
        feedback.NO_PENDING_CANCELLATION,
    }
)

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

    def take_except(self, key: ChatKey, keep: int | None, now: float) -> list[int]:
        """Remove and return the ids still young enough to delete, except ``keep``."""
        ids = self._by_chat.get(key)
        if not ids:
            return []
        kept = {keep: ids[keep]} if keep is not None and keep in ids else {}
        fresh = sorted(mid for mid, at in ids.items() if mid != keep and now - at < DELETE_WINDOW_SECONDS)
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


def _take(log: ThrowawayLog, key: ChatKey, keep: int | None, now: float) -> list[int]:
    ids = log.take_except(key, keep, now)
    writer.remove(key, ids)
    return ids


def is_cleanup_on() -> bool:
    return _cleanup_on.get()


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
        _log.add(key, int(message.message_id), time.monotonic())
        writer.add(key, int(message.message_id), KIND_THROWAWAY)


def keep_message(message: Message | None) -> None:
    """A panel now shows something the person should keep; never delete it."""
    key = chat_key(message) if message is not None else None
    if key is not None and getattr(message, "message_id", None) is not None:
        _log.forget(key, int(message.message_id))
        writer.remove(key, [int(message.message_id)])


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


async def replace_home(home: Message | None, *, sweep: bool = False) -> None:
    """A new home is on screen: delete the older homes, and with ``sweep`` the throwaway
    messages above it too.

    Call only after the new home was sent, so a message carrying the menu keyboard is
    always left in the chat. Sweep only when the home is the whole screen, not a keyboard
    attached under a panel that was just sent.
    """
    if home is None or not is_cleanup_on():
        return
    key = chat_key(home)
    home_id = getattr(home, "message_id", None)
    if key is None or home_id is None:
        return
    await _ensure_loaded(key)
    now = time.monotonic()
    stale = set(_take(_homes, key, None, now))
    if sweep:
        stale |= set(_take(_log, key, None, now))
    stale.discard(int(home_id))
    _homes.add(key, int(home_id), now)
    writer.add(key, int(home_id), KIND_HOME)
    await delete_messages(home.bot, key[1], sorted(stale))


async def delete_messages(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    for start in range(0, len(message_ids), DELETE_BATCH):
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=message_ids[start : start + DELETE_BATCH])
        except TelegramAPIError:
            # Already gone or too old: the chat is merely less tidy.
            logger.debug("chat cleanup could not delete %s messages", len(message_ids), exc_info=True)


async def _note_short_notices(
    make_request: NextRequestMiddlewareType[Any], bot: Bot, method: TelegramMethod[Any]
) -> Any:
    result = await make_request(bot, method)
    if isinstance(method, SendMessage) and isinstance(result, Message) and is_short_notice(method.text):
        note_throwaway(result, reply_markup=method.reply_markup, bot=bot)
    return result


def is_short_notice(text: str | None) -> bool:
    return (text or "").strip() in SHORT_NOTICES


def install_notice_watch(bot: Bot) -> Bot:
    """Treat the bot's fixed one-line notices («لغو شد.»، «⏳» …) as throwaway."""
    bot.session.middleware(_note_short_notices)
    return bot


class ChatCleanupMiddleware(BaseMiddleware):
    """Read this bot's switch once per update; note menu taps as throwaway."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        if session is None:
            return await handler(event, data)
        ui = await get_all_settings(session)
        token = _cleanup_on.set(is_chat_cleanup_on(ui))
        try:
            message = getattr(event, "message", None)
            if isinstance(message, Message) and is_menu_tap(message, ui):
                note_throwaway(message)
            return await handler(event, data)
        finally:
            _cleanup_on.reset(token)

