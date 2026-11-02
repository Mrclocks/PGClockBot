"""Database copy of the chat-cleanup list, so a restart does not forget what is on screen.

``app.bot.chat_cleanup`` keeps its list in memory for speed and mirrors every change here.
Changes are queued and written in small batches by one background task with its own short
session, never inside a handler's transaction, so a menu tap does not wait on the database and
a failed write only means a message stays in the chat. After a restart a chat's list is read
back once, on its first cleanup.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BotChatMessage
from app.db.session import SessionLocal
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)

KIND_THROWAWAY = "throwaway"
KIND_HOME = "home"
# Cleanup kinds; ``notice`` rows (``replaceable_notices``) are never swept by a panel or purged.
CLEANUP_KINDS = (KIND_THROWAWAY, KIND_HOME)
# Telegram refuses to delete a bot's message after 48 hours; rows older than this are useless.
DELETE_WINDOW = timedelta(hours=47)
# Rows read back for one chat; the in-memory list keeps fewer than this per chat anyway.
LOAD_LIMIT = 100
FLUSH_DELAY_SECONDS = 0.5
FLUSH_BATCH = 500
# A burst bigger than this between flushes is dropped: those messages simply stay in the chat.
QUEUE_LIMIT = 20_000
PURGE_BATCH = 5_000

ChatKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class StoredMessage:
    message_id: int
    kind: str
    sent_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LedgerWriter:
    """Queue of row additions and removals, written by one background task at a time."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession] = SessionLocal) -> None:
        self.sessions = sessions
        self._adds: dict[tuple[int, int, int], tuple[str, datetime]] = {}
        self._removes: set[tuple[int, int, int]] = set()
        self._task: asyncio.Task[None] | None = None

    def add(self, key: ChatKey, message_id: int, kind: str, sent_at: datetime | None = None) -> None:
        row = (key[0], key[1], int(message_id))
        if len(self._adds) >= QUEUE_LIMIT:
            return
        self._removes.discard(row)
        self._adds[row] = (kind, sent_at or _utcnow())
        self._schedule()

    def remove(self, key: ChatKey, message_ids: list[int]) -> None:
        for message_id in message_ids:
            row = (key[0], key[1], int(message_id))
            if self._adds.pop(row, None) is None and len(self._removes) < QUEUE_LIMIT:
                self._removes.add(row)
        self._schedule()

    def _schedule(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (a sync caller in a test): flushed by the next async change
        self._task = loop.create_task(self._flush_later(), name="chat-ledger-flush")

    async def _flush_later(self) -> None:
        await asyncio.sleep(FLUSH_DELAY_SECONDS)
        await self.flush()

    async def flush(self) -> None:
        """Write everything queued so far; on a database error the batch is dropped and logged."""
        while self._adds or self._removes:
            adds = list(self._adds.items())[:FLUSH_BATCH]
            removes = list(self._removes)[:FLUSH_BATCH]
            for row, _value in adds:
                del self._adds[row]
            self._removes.difference_update(removes)
            async with self.sessions() as session:
                try:
                    await _write(session, adds, removes)
                    await session.commit()
                except SQLAlchemyError:
                    # Only tidiness is lost: these messages are not deleted later.
                    logger.warning("chat ledger write failed adds=%s removes=%s", len(adds), len(removes), exc_info=True)
                    await rollback_quiet(session)


async def _write(
    session: AsyncSession, adds: list[tuple[tuple[int, int, int], tuple[str, datetime]]], removes: list[tuple[int, int, int]]
) -> None:
    if removes:
        await session.execute(
            delete(BotChatMessage).where(
                tuple_(BotChatMessage.bot_id, BotChatMessage.chat_id, BotChatMessage.message_id).in_(removes)
            )
        )
    if adds:
        values = [
            {"bot_id": b, "chat_id": c, "message_id": m, "kind": kind, "sent_at": at}
            for (b, c, m), (kind, at) in adds
        ]
        dialect = session.get_bind().dialect.name
        # The same message noted twice keeps its first row.
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        await session.execute(insert(BotChatMessage).values(values).on_conflict_do_nothing())


async def load_chat(session: AsyncSession, key: ChatKey, *, now: datetime | None = None) -> list[StoredMessage]:
    """The chat's stored messages young enough to delete, newest first, at most ``LOAD_LIMIT``."""
    cutoff = (now or _utcnow()) - DELETE_WINDOW
    rows = await session.execute(
        select(BotChatMessage.message_id, BotChatMessage.kind, BotChatMessage.sent_at)
        .where(
            BotChatMessage.bot_id == key[0],
            BotChatMessage.chat_id == key[1],
            BotChatMessage.kind.in_(CLEANUP_KINDS),
            BotChatMessage.sent_at > cutoff,
        )
        .order_by(BotChatMessage.message_id.desc())
        .limit(LOAD_LIMIT)
    )
    out = []
    for message_id, kind, sent_at in rows.all():
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        out.append(StoredMessage(message_id=int(message_id), kind=str(kind), sent_at=sent_at))
    return out


async def purge_expired(session: AsyncSession, *, now: datetime | None = None, limit: int = PURGE_BATCH) -> int:
    """Delete up to ``limit`` rows past the delete window; return how many."""
    cutoff = (now or _utcnow()) - DELETE_WINDOW
    ids = list(
        await session.scalars(
            select(BotChatMessage.id)
            .where(BotChatMessage.sent_at <= cutoff, BotChatMessage.kind.in_(CLEANUP_KINDS))
            .order_by(BotChatMessage.id)
            .limit(limit)
        )
    )
    if ids:
        await session.execute(delete(BotChatMessage).where(BotChatMessage.id.in_(ids)))
    await session.commit()
    return len(ids)


writer = LedgerWriter()
