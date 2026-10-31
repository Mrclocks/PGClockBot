"""The chat-cleanup list survives a restart: old welcomes and panels are still deleted."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import chat_cleanup, nav_inline
from app.bot.chat_cleanup import ThrowawayLog
from app.db import Base
from app.db.models import BotChatMessage
from app.services.chat_message_ledger import (
    KIND_HOME,
    KIND_THROWAWAY,
    LedgerWriter,
    load_chat,
    purge_expired,
)
from tests.test_chat_cleanup import _Chat

ROOT = Path(__file__).resolve().parents[1]
PANEL = InlineKeyboardMarkup(inline_keyboard=[])


class LedgerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'chat.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.writer = LedgerWriter(self.Session)
        self.restart()
        for name, value in (("writer", self.writer),):
            patcher = patch.object(chat_cleanup, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.token = chat_cleanup._cleanup_on.set(True)
        self.addCleanup(chat_cleanup._cleanup_on.reset, self.token)

    async def asyncTearDown(self) -> None:
        await self.writer.flush()
        await self.engine.dispose()
        self._tmp.cleanup()

    def restart(self) -> None:
        """A new process: the in-memory list is empty again."""
        for name, value in (("_log", ThrowawayLog()), ("_homes", ThrowawayLog(per_chat=5)), ("_loaded", OrderedDict())):
            patcher = patch.object(chat_cleanup, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def rows(self) -> list[tuple[int, str]]:
        async with self.Session() as session:
            result = await session.execute(select(BotChatMessage.message_id, BotChatMessage.kind).order_by(BotChatMessage.message_id))
            return [(int(m), str(k)) for m, k in result.all()]


class RestartTests(LedgerCase):
    async def test_after_a_restart_the_next_home_deletes_the_old_home_and_panels(self) -> None:
        chat = _Chat()
        old_home = chat.message()
        await chat_cleanup.replace_carrier(old_home)
        panel = await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        await self.writer.flush()
        self.restart()
        new_home = chat.message()
        await chat_cleanup.replace_carrier(new_home)
        await chat_cleanup.replace_home(new_home, sweep=True)
        self.assertIn(old_home.message_id, chat.deleted())
        self.assertIn(panel.message_id, chat.deleted())
        self.assertNotIn(new_home.message_id, chat.deleted())
        await self.writer.flush()
        self.assertEqual(await self.rows(), [(new_home.message_id, KIND_HOME)])

    async def test_after_a_restart_a_new_panel_deletes_the_old_panel(self) -> None:
        chat = _Chat()
        old = await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        await self.writer.flush()
        self.restart()
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertIn(old.message_id, chat.deleted())

    async def test_a_kept_message_is_not_deleted_after_a_restart(self) -> None:
        chat = _Chat()
        receipt = await nav_inline.present_inline_only(chat.message(from_bot=False), text="pay", inline=PANEL)
        chat_cleanup.keep_message(receipt)
        await self.writer.flush()
        self.restart()
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertNotIn(receipt.message_id, chat.deleted())

    async def test_the_stored_list_is_read_once_per_chat(self) -> None:
        chat = _Chat()
        await chat_cleanup.replace_home(chat.message(), sweep=True)
        await self.writer.flush()
        self.restart()
        with patch.object(chat_cleanup, "load_chat", AsyncMock(return_value=[])) as load:
            for _ in range(3):
                await nav_inline.present_inline_only(chat.message(from_bot=False), text="x", inline=PANEL)
        load.assert_awaited_once()

    async def test_another_chat_or_bot_is_never_touched(self) -> None:
        mine, other_chat, other_bot = _Chat(), _Chat(chat_id=200), _Chat(bot_id=2)
        for chat in (other_chat, other_bot):
            await chat_cleanup.replace_home(chat.message(), sweep=True)
        await self.writer.flush()
        self.restart()
        await chat_cleanup.replace_home(mine.message(), sweep=True)
        self.assertEqual(mine.deleted(), [])

    async def test_an_unreadable_database_only_skips_old_messages(self) -> None:
        chat = _Chat()
        self.restart()
        panel = await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        with patch.object(chat_cleanup, "load_chat", AsyncMock(side_effect=OperationalError("select", {}, Exception("x")))):
            await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertIn(panel.message_id, chat.deleted())

    async def test_nothing_is_stored_while_switched_off(self) -> None:
        chat = _Chat()
        chat_cleanup._cleanup_on.set(False)
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        await chat_cleanup.replace_home(chat.message(), sweep=True)
        await self.writer.flush()
        self.assertEqual(await self.rows(), [])


class WriterTests(LedgerCase):
    async def test_a_message_noted_twice_is_stored_once_and_removed(self) -> None:
        key = (1, 100)
        self.writer.add(key, 7, KIND_THROWAWAY)
        await self.writer.flush()
        self.writer.add(key, 7, KIND_THROWAWAY)
        await self.writer.flush()
        self.assertEqual(await self.rows(), [(7, KIND_THROWAWAY)])
        self.writer.remove(key, [7])
        await self.writer.flush()
        self.assertEqual(await self.rows(), [])

    async def test_a_failed_write_is_logged_and_dropped(self) -> None:
        await self.engine.dispose()
        broken = LedgerWriter(async_sessionmaker(create_async_engine("sqlite+aiosqlite:///:memory:")))
        broken.add((1, 100), 7, KIND_THROWAWAY)
        await broken.flush()  # the table does not exist there: logged, not raised


class ExpiryTests(LedgerCase):
    async def test_messages_past_the_delete_window_are_not_read_back_and_are_purged_in_batches(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all(
                [
                    BotChatMessage(bot_id=1, chat_id=100, message_id=i, kind=KIND_THROWAWAY, sent_at=now - timedelta(hours=50))
                    for i in range(1, 6)
                ]
                + [BotChatMessage(bot_id=1, chat_id=100, message_id=9, kind=KIND_HOME, sent_at=now)]
            )
            await session.commit()
            self.assertEqual([r.message_id for r in await load_chat(session, (1, 100))], [9])
            self.assertEqual(await purge_expired(session, limit=3), 3)
            self.assertEqual(await purge_expired(session, limit=3), 2)
            self.assertEqual(await purge_expired(session, limit=3), 0)
            self.assertEqual(await session.scalar(select(func.count()).select_from(BotChatMessage)), 1)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("chat_mig", ROOT / "alembic/versions/0068_bot_chat_messages.py")
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
            self.assertIn("bot_chat_messages", sa.inspect(conn).get_table_names())
            self.assertIn(
                "ix_bot_chat_messages_sent_at",
                {i["name"] for i in sa.inspect(conn).get_indexes("bot_chat_messages")},
            )
            migration.downgrade()
            self.assertNotIn("bot_chat_messages", sa.inspect(conn).get_table_names())
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
