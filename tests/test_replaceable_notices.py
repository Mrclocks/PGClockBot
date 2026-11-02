"""A new system alert replaces the previous one of its kind; a cleared alert removes its message."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotChatMessage
from app.jobs import panel_alert_notifications as job
from app.services import panel_alert_notifications as svc
from app.services.chat_message_ledger import KIND_THROWAWAY, load_chat, purge_expired
from app.services.replaceable_notices import (
    KIND_NOTICE,
    REPLACED_TEXT,
    RESOLVED_TEXT,
    alert_replace_key,
    clear_notices,
    replace_notices,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = alert_replace_key("update")
NOW = datetime.now(timezone.utc)


def fake_bot(bot_id: int = 1) -> MagicMock:
    return MagicMock(id=bot_id, delete_message=AsyncMock(), edit_message_text=AsyncMock())


def sent(chat_id: int, message_id: int) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)


class NoticeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'n.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def stored(self) -> list[tuple[int, int, int, str | None]]:
        async with self.Session() as session:
            rows = await session.execute(
                select(BotChatMessage.bot_id, BotChatMessage.chat_id, BotChatMessage.message_id, BotChatMessage.replace_key)
                .order_by(BotChatMessage.id)
            )
            return [tuple(r) for r in rows.all()]

    async def replace(self, bot, messages, key=KEY) -> None:
        async with self.Session() as session:
            await replace_notices(session, bot, key, messages)


class ReplaceTests(NoticeCase):
    async def test_the_next_alert_deletes_the_previous_one_in_each_chat(self) -> None:
        bot = fake_bot()
        await self.replace(bot, [sent(10, 5), sent(20, 6)])
        await self.replace(bot, [sent(10, 9), sent(20, 12)])
        deleted = sorted((c.kwargs["chat_id"], c.kwargs["message_id"]) for c in bot.delete_message.await_args_list)
        self.assertEqual(deleted, [(10, 5), (20, 6)])
        self.assertEqual(await self.stored(), [(1, 10, 9, KEY), (1, 20, 12, KEY)])

    async def test_an_alert_too_old_to_delete_is_shortened_instead(self) -> None:
        async with self.Session() as session:
            session.add(BotChatMessage(bot_id=1, chat_id=10, message_id=5, kind=KIND_NOTICE,
                                       sent_at=NOW - timedelta(days=5), replace_key=KEY))
            await session.commit()
        bot = fake_bot()
        await self.replace(bot, [sent(10, 9)])
        bot.delete_message.assert_not_awaited()
        self.assertEqual(bot.edit_message_text.await_args.kwargs["text"], REPLACED_TEXT)
        self.assertEqual(bot.edit_message_text.await_args.kwargs["message_id"], 5)

    async def test_other_kinds_bots_and_chats_are_left_alone(self) -> None:
        bot, other_bot = fake_bot(1), fake_bot(2)
        await self.replace(bot, [sent(10, 5)], key=alert_replace_key("capacity"))
        await self.replace(other_bot, [sent(10, 6)])
        await self.replace(bot, [sent(30, 7)])
        await self.replace(bot, [sent(10, 9)])
        bot.delete_message.assert_not_awaited()
        other_bot.delete_message.assert_not_awaited()

    async def test_a_message_the_person_already_deleted_does_not_break_anything(self) -> None:
        bot = fake_bot()
        bot.delete_message.side_effect = TelegramBadRequest(MagicMock(), "message to delete not found")
        await self.replace(bot, [sent(10, 5)])
        await self.replace(bot, [sent(10, 9)])
        self.assertEqual(await self.stored(), [(1, 10, 9, KEY)])


class DispatchTests(NoticeCase):
    async def test_dispatching_with_a_replace_key_replaces_the_previous_alert(self) -> None:
        from app.services import notifications

        bot = fake_bot()
        next_id = iter([5, 9])

        async def send_to_chats(send_bot, targets, text, *, markup=None, photo=None, sent_messages=None):
            for chat_id in targets:
                sent_messages.append(sent(chat_id, next(next_id)))
            return len(targets)

        with patch.object(notifications, "_send_to_chats", send_to_chats), \
                patch.object(notifications, "notify_enabled", AsyncMock(return_value=True)), \
                patch.object(notifications, "get_settings", return_value=SimpleNamespace(admin_ids=[10])):
            for _ in range(2):
                async with self.Session() as session:
                    delivered = await notifications._dispatch_dual_notify(
                        bot, session, svc.KEY_UPDATE, "t", shop=False, replace_key=KEY
                    )
                self.assertEqual(delivered, 1)
        self.assertEqual(bot.delete_message.await_args.kwargs["message_id"], 5)
        self.assertEqual(await self.stored(), [(1, 10, 9, KEY)])


class ClearTests(NoticeCase):
    async def test_a_cleared_alert_removes_its_last_message_everywhere(self) -> None:
        bot = fake_bot()
        await self.replace(bot, [sent(10, 5), sent(20, 6)])
        async with self.Session() as session:
            session.add(BotChatMessage(bot_id=1, chat_id=30, message_id=2, kind=KIND_NOTICE,
                                       sent_at=NOW - timedelta(days=3), replace_key=KEY))
            await session.commit()
            self.assertEqual(await clear_notices(session, bot, KEY), 3)
        self.assertEqual(bot.delete_message.await_count, 2)
        self.assertEqual(bot.edit_message_text.await_args.kwargs["text"], RESOLVED_TEXT)
        self.assertEqual(await self.stored(), [])


class CleanupSeparationTests(NoticeCase):
    async def test_alerts_are_not_swept_by_panels_and_not_purged(self) -> None:
        async with self.Session() as session:
            session.add_all([
                BotChatMessage(bot_id=1, chat_id=10, message_id=5, kind=KIND_NOTICE,
                               sent_at=NOW - timedelta(days=3), replace_key=KEY),
                BotChatMessage(bot_id=1, chat_id=10, message_id=6, kind=KIND_NOTICE, sent_at=NOW, replace_key=KEY),
                BotChatMessage(bot_id=1, chat_id=10, message_id=7, kind=KIND_THROWAWAY, sent_at=NOW),
            ])
            await session.commit()
            self.assertEqual([r.message_id for r in await load_chat(session, (1, 10))], [7])
            self.assertEqual(await purge_expired(session), 0)
        self.assertEqual(len(await self.stored()), 3)


class JobTests(NoticeCase):
    def sig(self, count: int, fingerprint: str = "0.5.2") -> svc.Signal:
        return svc.Signal(shop_id=None, alert=svc.ALERT_UPDATE, notify_key=svc.KEY_UPDATE, count=count,
                          text="t", path="/x", fingerprint=fingerprint)

    async def announce(self, signal: svc.Signal, bot: MagicMock) -> AsyncMock:
        dispatch = AsyncMock(return_value=1)
        async with self.Session() as session:
            with patch.object(job, "_dispatch_dual_notify", dispatch), patch.object(job, "SEND_PAUSE_SECONDS", 0):
                await job.announce(bot, session, signal)
        return dispatch

    async def test_an_alert_is_sent_as_replaceable_and_its_clearing_removes_it(self) -> None:
        bot = fake_bot()
        dispatch = await self.announce(self.sig(1), bot)
        self.assertEqual(dispatch.await_args.kwargs["replace_key"], KEY)
        await self.replace(bot, [sent(10, 5)])  # what the dispatch stored for its message
        with patch.object(job, "clear_notices", AsyncMock(wraps=clear_notices)) as clear:
            await self.announce(self.sig(0), bot)
            await self.announce(self.sig(0), bot)  # already off: nothing more to clear
        clear.assert_awaited_once()
        self.assertEqual(bot.delete_message.await_args.kwargs["message_id"], 5)

    async def test_a_shop_alert_is_cleared_from_that_shops_bot(self) -> None:
        shop_bot = fake_bot(7)
        shop_bot.session = MagicMock(close=AsyncMock())
        signal = svc.Signal(shop_id=5, alert=svc.ALERT_FAILURES, notify_key=svc.KEY_FAILURES, count=1, text="t", path="/x")
        await self.announce(signal, fake_bot())
        cleared = svc.Signal(shop_id=5, alert=svc.ALERT_FAILURES, notify_key=svc.KEY_FAILURES, count=0, text="t", path="/x")
        with patch.object(job, "open_notify_bot_for_reseller", AsyncMock(return_value=(shop_bot, True))) as opener, \
                patch.object(job, "clear_notices", AsyncMock(return_value=0)) as clear:
            await self.announce(cleared, fake_bot())
        opener.assert_awaited_once()
        self.assertIs(clear.await_args.args[1], shop_bot)
        shop_bot.session.close.assert_awaited_once()


class MigrationTests(unittest.TestCase):
    def load(self, name: str):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"alembic/versions/{name}.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_upgrade_twice_and_downgrade(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        base, migration = self.load("0068_bot_chat_messages"), self.load("0069_bot_chat_message_replace_key")
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            base.upgrade()
            conn.execute(sa.text(
                "INSERT INTO bot_chat_messages (bot_id, chat_id, message_id, kind, sent_at) VALUES (1, 2, 3, 'home', '2026-10-25')"
            ))
            migration.upgrade()
            migration.upgrade()
            self.assertIn("replace_key", {c["name"] for c in sa.inspect(conn).get_columns("bot_chat_messages")})
            migration.downgrade()
            self.assertNotIn("replace_key", {c["name"] for c in sa.inspect(conn).get_columns("bot_chat_messages")})
            self.assertEqual(conn.execute(sa.text("SELECT count(*) FROM bot_chat_messages")).scalar(), 1)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
