"""System alerts and daily reports that replace each other instead of piling up in a staff chat.

Each alert kind has a ``replace_key`` (for example ``alert:update``, ``report:daily``). When the next alert of that
kind reaches a chat, the previous one in the same chat is deleted; one older than Telegram's
48-hour delete window is edited into a one-line «old alert» note instead, since a bot may edit its
own messages at any time. When the alert clears (the panel was updated, the failures were handled)
the last one is removed the same way. The database is read and written in short transactions on
either side of the Telegram calls, never across them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotChatMessage
from app.services.chat_message_ledger import DELETE_WINDOW
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)

KIND_NOTICE = "notice"
REPLACED_TEXT = "🔕 این اعلان قدیمی است؛ اعلان جدیدتر پایین‌تر آمده است."
REPORT_REPLACED_TEXT = "🗂 این گزارش قدیمی است؛ گزارش جدیدتر پایین‌تر آمده است."
DAILY_REPORT_KEY = "report:daily"
SUBORDINATE_REPORT_KEY = "report:subordinate"
RESOLVED_TEXT = "✅ این هشدار برطرف شده است."
# Bounds one clear of an alert for a bot (one row per staff chat).
CLEAR_LIMIT = 500


@dataclass(frozen=True, slots=True)
class _Stored:
    row_id: int
    chat_id: int
    message_id: int
    sent_at: datetime


def alert_replace_key(alert: str) -> str:
    return f"alert:{alert}"[:64]


async def _stored(session: AsyncSession, bot_id: int, replace_key: str, chat_ids: list[int] | None) -> list[_Stored]:
    query = select(BotChatMessage.id, BotChatMessage.chat_id, BotChatMessage.message_id, BotChatMessage.sent_at).where(
        BotChatMessage.bot_id == int(bot_id), BotChatMessage.replace_key == replace_key
    )
    if chat_ids is not None:
        query = query.where(BotChatMessage.chat_id.in_([int(c) for c in chat_ids]))
    rows = (await session.execute(query.order_by(BotChatMessage.id).limit(CLEAR_LIMIT))).all()
    await session.commit()  # end the read before Telegram is called
    out = []
    for row_id, chat_id, message_id, sent_at in rows:
        sent_at = sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=timezone.utc)
        out.append(_Stored(int(row_id), int(chat_id), int(message_id), sent_at))
    return out


async def _retire(bot: Bot, stored: _Stored, note: str, now: datetime) -> None:
    """Delete the old alert, or shorten it to ``note`` when it is too old to delete."""
    try:
        if now - stored.sent_at < DELETE_WINDOW:
            await bot.delete_message(chat_id=stored.chat_id, message_id=stored.message_id)
        else:
            await bot.edit_message_text(text=note, chat_id=stored.chat_id, message_id=stored.message_id, reply_markup=None)
    except TelegramAPIError:
        # Already deleted by the person, or not editable: nothing left to tidy.
        logger.debug("alert retire failed chat=%s message=%s", stored.chat_id, stored.message_id, exc_info=True)


async def _forget(session: AsyncSession, row_ids: list[int]) -> None:
    if row_ids:
        await session.execute(delete(BotChatMessage).where(BotChatMessage.id.in_(row_ids)))


async def replace_notices(
    session: AsyncSession, bot: Bot, replace_key: str, sent: list[Message], *, replaced_text: str = REPLACED_TEXT
) -> None:
    """``sent`` just reached their chats: retire the previous alert of this kind in each of them."""
    chats = {int(m.chat.id): m for m in sent if getattr(m, "chat", None) is not None}
    if not chats:
        return
    try:
        previous = await _stored(session, int(bot.id), replace_key, list(chats))
    except SQLAlchemyError:
        logger.warning("alert replace: read failed key=%s", replace_key, exc_info=True)
        await rollback_quiet(session)
        return
    now = datetime.now(timezone.utc)
    for stored in previous:
        if stored.message_id != chats[stored.chat_id].message_id:
            await _retire(bot, stored, replaced_text, now)
    try:
        await _forget(session, [s.row_id for s in previous])
        session.add_all(
            BotChatMessage(
                bot_id=int(bot.id), chat_id=chat_id, message_id=int(message.message_id),
                kind=KIND_NOTICE, sent_at=now, replace_key=replace_key,
            )
            for chat_id, message in chats.items()
        )
        await session.commit()
    except SQLAlchemyError:
        # The new alert is shown; only the next replacement will not find it.
        logger.warning("alert replace: write failed key=%s", replace_key, exc_info=True)
        await rollback_quiet(session)


async def clear_notices(
    session: AsyncSession, bot: Bot, replace_key: str, *, resolved_text: str = RESOLVED_TEXT
) -> int:
    """The alert is over: retire its last message in every chat of this bot; return how many."""
    try:
        stored = await _stored(session, int(bot.id), replace_key, None)
    except SQLAlchemyError:
        logger.warning("alert clear: read failed key=%s", replace_key, exc_info=True)
        await rollback_quiet(session)
        return 0
    now = datetime.now(timezone.utc)
    for item in stored:
        await _retire(bot, item, resolved_text, now)
    try:
        await _forget(session, [s.row_id for s in stored])
        await session.commit()
    except SQLAlchemyError:
        logger.warning("alert clear: write failed key=%s", replace_key, exc_info=True)
        await rollback_quiet(session)
    return len(stored)


async def send_replacing_report(
    session: AsyncSession, bot: Bot, chat_ids: list[int], text: str, *, replace_key: str, **send_kw: Any
) -> int:
    """Send a report to each chat and retire that chat's previous report of the same kind.

    Returns how many chats received it; a chat that cannot be reached is skipped and logged.
    """
    sent: list[Message] = []
    for chat_id in chat_ids:
        try:
            sent.append(await bot.send_message(int(chat_id), text, **send_kw))
        except TelegramAPIError:
            logger.warning("report send failed key=%s chat=%s", replace_key, chat_id, exc_info=True)
    await replace_notices(session, bot, replace_key, sent, replaced_text=REPORT_REPLACED_TEXT)
    return len(sent)
