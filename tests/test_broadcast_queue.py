"""Durable owner broadcasts: audience scope, no double sends, unknown outcomes, cancel and migration."""
import asyncio
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.broadcast_queue_pages import register_broadcast_queue_pages
from app.db.models import Base, BotUser, BroadcastLog
from app.services.broadcast import clear_broadcast_history, delete_broadcast_log
from app.services.broadcast_queue import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    cancel_broadcast,
    process_broadcast_batch,
    queue_broadcast,
    run_broadcast_worker,
)

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_USERS = (1, 2, 3, 4, 5)  # telegram ids 1001..1005; the shop owner (9020) is a reseller-role platform customer too
EVERYONE = [1001, 1002, 1003, 1004, 1005, 9020]
SHOP_CUSTOMER_TG = 2001


class FakeBot:
    def __init__(self):
        self.sent: list[int] = []
        self.behaviour = {}
        self.after_send = None

    async def send_message(self, chat_id, body, parse_mode=None):
        action = self.behaviour.get(chat_id)
        if isinstance(action, Exception):
            raise action
        self.sent.append(chat_id)
        if self.after_send:
            await self.after_send(chat_id)


class QueueCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'broadcast.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add(BotUser(id=20, telegram_id=9020, referral_code="shop", role="reseller"))
        await self.session.flush()
        self.session.add_all([BotUser(id=i, telegram_id=1000 + i, referral_code=f"u{i}", role="user") for i in PLATFORM_USERS])
        self.session.add_all([
            BotUser(id=10, telegram_id=SHOP_CUSTOMER_TG, referral_code="shopcust", role="user", reseller_id=20),
            BotUser(id=11, telegram_id=3001, referral_code="blocked", role="user", is_blocked=True),
            BotUser(id=12, telegram_id=3002, referral_code="admin", role="admin"),
        ])
        await self.session.commit()
        self.bot = FakeBot()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def log(self, log_id):
        row = await self.session.get(BroadcastLog, log_id)
        await self.session.refresh(row)
        return row

    async def run_all(self, log_id, **kw):
        for _ in range(50):
            async with self.Session() as session:
                if not await process_broadcast_batch(session, log_id, self.bot, delay=0, **kw):
                    return


class QueueTests(QueueCase):
    async def test_queue_validates_and_counts_only_the_platform_audience(self):
        for kwargs in ({"text": "  ", "audience": "all"}, {"text": "x" * 4001, "audience": "all"}, {"text": "hi", "audience": "everyone"}):
            with self.assertRaises(ValueError):
                await queue_broadcast(self.session, created_by="owner", **kwargs)
        self.assertEqual((await queue_broadcast(self.session, text="hi", audience="all", created_by="owner")).total, 7)
        self.assertEqual((await queue_broadcast(self.session, text="hi", audience="users", created_by="owner")).total, 6)
        self.assertEqual((await queue_broadcast(self.session, text="hi", audience="resellers", created_by="owner")).total, 1)
        self.assertEqual((await queue_broadcast(self.session, text="hi", audience="admins", created_by="owner")).total, 1)
        await self.session.execute(BotUser.__table__.update().where(BotUser.role == "admin").values(role="user"))
        await self.session.commit()
        with self.assertRaises(ValueError):
            await queue_broadcast(self.session, text="hi", audience="admins", created_by="owner")

    async def test_every_platform_user_is_messaged_exactly_once_across_batches(self):
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        async with self.Session() as session:
            self.assertTrue(await process_broadcast_batch(session, log.id, self.bot, batch_size=2, delay=0))
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual((await self.log(log.id)).status, STATUS_RUNNING)
        await self.run_all(log.id, batch_size=2)
        self.assertEqual(sorted(self.bot.sent), EVERYONE)
        row = await self.log(log.id)
        self.assertEqual((row.status, row.ok_count, row.fail_count, row.review_count), (STATUS_DONE, 6, 0, 0))
        self.assertIsNotNone(row.completed_at)

    async def test_shop_customers_blocked_users_and_other_roles_are_never_messaged(self):
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        await self.run_all(log.id)
        self.assertNotIn(SHOP_CUSTOMER_TG, self.bot.sent)
        self.assertNotIn(3001, self.bot.sent)
        self.assertNotIn(3002, self.bot.sent)

    async def test_blocked_bot_counts_as_failed_and_the_send_continues(self):
        self.bot.behaviour[1002] = TelegramForbiddenError(method=MagicMock(), message="blocked")
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        await self.run_all(log.id)
        row = await self.log(log.id)
        self.assertEqual((row.ok_count, row.fail_count, row.status), (5, 1, STATUS_DONE))

    async def test_an_unknown_outcome_is_counted_and_never_retried(self):
        self.bot.behaviour[1003] = TimeoutError("lost response")
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        await self.run_all(log.id)
        await self.run_all(log.id)
        row = await self.log(log.id)
        self.assertEqual((row.ok_count, row.review_count, row.fail_count), (5, 1, 0))
        self.assertEqual(self.bot.sent.count(1003), 0)

    async def test_retry_after_pauses_without_skipping_or_counting_the_user(self):
        self.bot.behaviour[1002] = TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=30)
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        async with self.Session() as session:
            self.assertTrue(await process_broadcast_batch(session, log.id, self.bot, delay=0))
        row = await self.log(log.id)
        self.assertEqual(self.bot.sent, [1001])
        self.assertEqual((row.ok_count, row.cursor_id, row.inflight_id), (1, 1, None))
        self.assertGreater(row.locked_until.replace(tzinfo=timezone.utc), datetime.now(timezone.utc) + timedelta(seconds=20))
        async with self.Session() as session:
            self.assertFalse(await process_broadcast_batch(session, log.id, self.bot, delay=0))
        self.assertEqual(self.bot.sent, [1001])
        await self.session.execute(BroadcastLog.__table__.update().values(locked_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await self.session.commit()
        del self.bot.behaviour[1002]
        await self.run_all(log.id)
        self.assertEqual(sorted(self.bot.sent), EVERYONE)
        self.assertEqual((await self.log(log.id)).ok_count, 6)

    async def test_a_user_left_in_flight_by_a_dead_worker_is_skipped_as_unknown(self):
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        await self.session.execute(BroadcastLog.__table__.update().values(
            status=STATUS_RUNNING, cursor_id=2, inflight_id=3, lock_token="dead", locked_until=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await self.session.commit()
        await self.run_all(log.id)
        self.assertEqual(sorted(self.bot.sent), [1004, 1005, 9020])
        row = await self.log(log.id)
        self.assertEqual((row.review_count, row.ok_count, row.status), (1, 3, STATUS_DONE))

    async def test_concurrent_workers_never_message_a_user_twice(self):
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")

        async def worker():
            async with self.Session() as session:
                await process_broadcast_batch(session, log.id, self.bot, batch_size=10, delay=0.01)

        await asyncio.gather(worker(), worker(), worker())
        self.assertEqual(sorted(self.bot.sent), EVERYONE)

    async def test_cancel_stops_a_queued_broadcast_and_one_that_is_running(self):
        queued = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        self.assertTrue(await cancel_broadcast(self.session, queued.id))
        self.assertFalse(await cancel_broadcast(self.session, queued.id))
        async with self.Session() as session:
            self.assertFalse(await process_broadcast_batch(session, queued.id, self.bot, delay=0))
        self.assertEqual(self.bot.sent, [])
        running = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")

        async def cancel_after_first(chat_id):
            async with self.Session() as session:
                await cancel_broadcast(session, running.id)

        self.bot.after_send = cancel_after_first
        async with self.Session() as session:
            await process_broadcast_batch(session, running.id, self.bot, delay=0)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual((await self.log(running.id)).status, STATUS_CANCELLED)

    async def test_history_cannot_delete_or_clear_an_active_broadcast(self):
        active = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        finished = await queue_broadcast(self.session, text="old", audience="users", created_by="owner")
        await cancel_broadcast(self.session, finished.id)
        self.assertFalse(await delete_broadcast_log(self.session, active.id))
        self.assertEqual(await clear_broadcast_history(self.session), 1)
        self.session.expunge_all()
        self.assertIsNotNone(await self.session.get(BroadcastLog, active.id))
        self.assertIsNone(await self.session.get(BroadcastLog, finished.id))

    async def test_the_tick_worker_drains_every_active_broadcast_within_its_budget(self):
        first = await queue_broadcast(self.session, text="a", audience="users", created_by="owner")
        second = await queue_broadcast(self.session, text="b", audience="admins", created_by="owner")
        await run_broadcast_worker(self.Session, self.bot, budget_seconds=20)
        self.assertEqual((await self.log(first.id)).status, STATUS_DONE)
        self.assertEqual((await self.log(second.id)).status, STATUS_DONE)

    async def test_the_tick_worker_stops_at_its_budget(self):
        await queue_broadcast(self.session, text="a", audience="users", created_by="owner")
        await run_broadcast_worker(self.Session, self.bot, budget_seconds=0)
        self.assertEqual(self.bot.sent, [])


class HistoryTemplateTests(unittest.TestCase):
    def render(self, rows):
        from types import SimpleNamespace as NS

        from app.api.app import templates
        from app.services.broadcast import AUDIENCE_LABELS
        from app.services.broadcast_queue import STATUS_LABELS

        request = NS(url=NS(path="/broadcast"), query_params={}, state=NS(), cookies={}, headers={})
        return templates.env.get_template("broadcast.html").render(
            request=request, staff={"role": "admin"}, history=rows, audience_labels=AUDIENCE_LABELS,
            status_labels=STATUS_LABELS, flash_ok=None, flash_err=None, csp_nonce="n", csrf_token="TOK",
        )

    def row(self, row_id, status, text="hello"):
        from types import SimpleNamespace as NS

        return NS(id=row_id, status=status, audience="all", text=text, total=10, ok_count=3, fail_count=1,
                  review_count=2, created_by="owner", created_at="2026-01-01 10:00:00")

    def test_an_active_send_can_be_stopped_and_only_a_finished_one_can_be_deleted(self):
        html = self.render([self.row(1, "running"), self.row(2, "done"), self.row(3, "queued")])
        self.assertIn('action="/broadcast/history/1/cancel"', html)
        self.assertIn('action="/broadcast/history/3/cancel"', html)
        self.assertNotIn('action="/broadcast/history/1/delete"', html)
        self.assertIn('action="/broadcast/history/2/delete"', html)
        self.assertNotIn('action="/broadcast/history/2/cancel"', html)
        self.assertIn("نامشخص", html)

    def test_message_text_is_escaped(self):
        html = self.render([self.row(1, "done", text="<script>alert(1)</script>")])
        self.assertNotIn("<script>alert(1)</script>", html)


class CancelRouteTests(QueueCase):
    async def post(self, log_id, *, admin=True):
        app = FastAPI()

        async def db():
            yield self.session

        async def require_admin():
            if not admin:
                from fastapi import HTTPException

                raise HTTPException(403)
            return {"role": "admin"}

        register_broadcast_queue_pages(app, require_admin=require_admin, get_db=db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(f"/broadcast/history/{log_id}/cancel", follow_redirects=False)

    async def test_only_an_admin_can_cancel_and_a_finished_one_reports_it(self):
        log = await queue_broadcast(self.session, text="hello", audience="users", created_by="owner")
        self.assertEqual((await self.post(log.id, admin=False)).status_code, 403)
        self.assertEqual((await self.log(log.id)).status, STATUS_QUEUED)
        self.assertIn("ok=", (await self.post(log.id)).headers["location"])
        self.assertIn("err=", (await self.post(log.id)).headers["location"])


class BroadcastMigrationTests(unittest.TestCase):
    def test_upgrade_twice_marks_old_rows_done_and_downgrade_keeps_them(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("broadcast_migration", ROOT / "alembic/versions/0073_broadcast_queue.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, audience TEXT, text TEXT, total INTEGER, ok_count INTEGER, fail_count INTEGER)"))
            conn.execute(text("INSERT INTO broadcast_logs VALUES (1, 'all', 'old message', 10, 9, 1)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(conn.execute(text("SELECT status, cursor_id, review_count FROM broadcast_logs")).one(), ("done", 0, 0))
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT text, ok_count FROM broadcast_logs")).one(), ("old message", 9))
                self.assertNotIn("status", {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")})
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
