"""Shop-scoped group messages: audience, tenancy, authz, daily cap, idempotency."""

from __future__ import annotations

import asyncio
import html
import importlib.util
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from fastapi import FastAPI, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.shop_broadcast_pages import register_shop_broadcast_pages, shop_broadcast_scope
from app.db.models import (
    Base,
    BotUser,
    BotUserShop,
    BroadcastLog,
    ResellerProfile,
    ShopBroadCampaignDay,
)
from app.services.broadcast import clear_broadcast_history, delete_broadcast_log, list_broadcast_history
from app.services.broadcast_queue import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    cancel_broadcast,
    process_broadcast_batch,
    queue_broadcast,
    run_broadcast_worker,
)
from app.services.campaigns import set_marketing_preference
from app.services.shop_broadcast import (
    SHOP_NO_BOT_MESSAGE,
    clean_shop_broadcast_text,
    count_shop_broadcast_targets,
    new_shop_request_key,
    queue_shop_broadcast,
    shop_audience_clause,
    shop_broadcast_html_snippet,
)

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 20
SHOP_B = 30


class FakeBot:
    def __init__(self):
        self.sent: list[int] = []
        self.bodies: list[str] = []
        self.behaviour = {}

    async def send_message(self, chat_id, body, parse_mode=None):
        action = self.behaviour.get(chat_id)
        if isinstance(action, Exception):
            raise action
        self.sent.append(chat_id)
        self.bodies.append(body)


class ShopBroadcastCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'shop_bc.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all(
            [
                BotUser(id=SHOP_A, telegram_id=9020, referral_code="shopa", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=9030, referral_code="shopb", role="reseller"),
                BotUser(id=1, telegram_id=1001, referral_code="p1", role="user"),
                BotUser(id=2, telegram_id=1002, referral_code="p2", role="user"),
                BotUser(id=3, telegram_id=2001, referral_code="a1", role="user", reseller_id=SHOP_A),
                BotUser(id=4, telegram_id=2002, referral_code="a2", role="user", reseller_id=SHOP_A),
                BotUser(id=5, telegram_id=3001, referral_code="b1", role="user", reseller_id=SHOP_B),
                BotUser(id=6, telegram_id=2003, referral_code="both", role="user", reseller_id=SHOP_A),
                BotUser(id=7, telegram_id=2004, referral_code="blocked", role="user", reseller_id=SHOP_A, is_blocked=True),
                BotUser(id=8, telegram_id=2005, referral_code="optout", role="user", reseller_id=SHOP_A),
                BotUser(id=9, telegram_id=2006, referral_code="shopblock", role="user", reseller_id=SHOP_A),
                BotUser(id=10, telegram_id=4001, referral_code="admin", role="admin"),
            ]
        )
        await self.session.flush()
        self.session.add_all(
            [
                BotUserShop(bot_user_id=6, shop_id=SHOP_B),
                BotUserShop(bot_user_id=9, shop_id=SHOP_A, is_blocked=True, blocked_reason="test"),
                ResellerProfile(user_id=SHOP_A, bot_token="enc-shop-a", is_active=True),
                ResellerProfile(user_id=SHOP_B, bot_token="enc-shop-b", is_active=True),
            ]
        )
        await self.session.commit()
        await set_marketing_preference(self.session, 8, shop_id=SHOP_A, enabled=False)
        self.bot = FakeBot()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def run_all(self, log_id, bot=None, **kw):
        bot = bot or self.bot
        for _ in range(50):
            async with self.Session() as session:
                if not await process_broadcast_batch(session, log_id, bot, delay=0, **kw):
                    return


class AudienceTests(ShopBroadcastCase):
    async def test_shop_audience_is_only_that_shops_customers(self):
        total = await count_shop_broadcast_targets(self.session, shop_id=SHOP_A)
        # users 3,4,6 — blocked global, opt-out, shop-blocked excluded; platform and shop B out
        self.assertEqual(total, 3)
        ids = list(
            (
                await self.session.scalars(select(BotUser.id).where(*shop_audience_clause(SHOP_A)).order_by(BotUser.id))
            ).all()
        )
        self.assertEqual(ids, [3, 4, 6])

    async def test_member_of_two_shops_is_only_messaged_from_sending_shop(self):
        log_a = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="from A", created_by="a", request_key=new_shop_request_key()
        )
        await self.run_all(log_a.id)
        self.assertIn(2003, self.bot.sent)
        self.assertNotIn(3001, self.bot.sent)
        self.assertNotIn(1001, self.bot.sent)

        self.bot.sent.clear()
        log_b = await queue_shop_broadcast(
            self.session, shop_id=SHOP_B, text="from B", created_by="b", request_key=new_shop_request_key()
        )
        await self.run_all(log_b.id)
        self.assertIn(2003, self.bot.sent)  # dual member via BotUserShop
        self.assertIn(3001, self.bot.sent)
        self.assertNotIn(2001, self.bot.sent)

    async def test_owner_broadcast_never_reaches_shop_customers(self):
        log = await queue_broadcast(self.session, text="platform", audience="users", created_by="owner")
        await self.run_all(log.id)
        self.assertNotIn(2001, self.bot.sent)
        self.assertNotIn(3001, self.bot.sent)
        self.assertIn(1001, self.bot.sent)

    async def test_shop_broadcast_never_reaches_platform_or_admins(self):
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="shop", created_by="a", request_key=new_shop_request_key()
        )
        await self.run_all(log.id)
        self.assertNotIn(1001, self.bot.sent)
        self.assertNotIn(4001, self.bot.sent)
        self.assertNotIn(9020, self.bot.sent)


class TenancyHistoryTests(ShopBroadcastCase):
    async def test_history_clear_and_delete_are_tenant_isolated(self):
        owner = await queue_broadcast(self.session, text="owner", audience="users", created_by="owner")
        shop = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="shop", created_by="a", request_key=new_shop_request_key()
        )
        await cancel_broadcast(self.session, owner.id, shop_id=None)
        await cancel_broadcast(self.session, shop.id, shop_id=SHOP_A)

        owner_hist = await list_broadcast_history(self.session, shop_id=None)
        shop_hist = await list_broadcast_history(self.session, shop_id=SHOP_A)
        other = await list_broadcast_history(self.session, shop_id=SHOP_B)
        self.assertEqual([row.id for row in owner_hist], [owner.id])
        self.assertEqual([row.id for row in shop_hist], [shop.id])
        self.assertEqual(other, [])

        self.assertFalse(await delete_broadcast_log(self.session, shop.id, shop_id=None))
        self.assertFalse(await delete_broadcast_log(self.session, owner.id, shop_id=SHOP_A))
        self.assertFalse(await delete_broadcast_log(self.session, shop.id, shop_id=SHOP_B))
        # Owner clear must not remove the finished shop row.
        self.assertEqual(await clear_broadcast_history(self.session, shop_id=None), 1)
        self.assertIsNone(
            await self.session.scalar(select(BroadcastLog.id).where(BroadcastLog.id == owner.id))
        )
        self.assertIsNotNone(
            await self.session.scalar(select(BroadcastLog.id).where(BroadcastLog.id == shop.id))
        )
        self.assertTrue(await delete_broadcast_log(self.session, shop.id, shop_id=SHOP_A))
        self.assertIsNone(
            await self.session.scalar(select(BroadcastLog.id).where(BroadcastLog.id == shop.id))
        )

    async def test_cancel_is_scoped_at_mutation_time(self):
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="x", created_by="a", request_key=new_shop_request_key()
        )
        self.assertFalse(await cancel_broadcast(self.session, log.id, shop_id=SHOP_B))
        self.assertFalse(await cancel_broadcast(self.session, log.id, shop_id=None))
        self.assertTrue(await cancel_broadcast(self.session, log.id, shop_id=SHOP_A))
        status = await self.session.scalar(
            select(BroadcastLog.status).where(BroadcastLog.id == log.id)
        )
        self.assertEqual(status, STATUS_CANCELLED)


class CapAndIdempotencyTests(ShopBroadcastCase):
    async def test_daily_broad_cap_rejects_a_second_shop_broadcast(self):
        first = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="one", created_by="a", request_key=new_shop_request_key()
        )
        self.assertEqual(first.status, STATUS_QUEUED)
        with self.assertRaises(ValueError):
            await queue_shop_broadcast(
                self.session, shop_id=SHOP_A, text="two", created_by="a", request_key=new_shop_request_key()
            )

    async def test_concurrent_claims_only_one_broadcast(self):
        async def attempt(nonce: str):
            async with self.Session() as session:
                try:
                    return await queue_shop_broadcast(
                        session, shop_id=SHOP_A, text="race", created_by="a", request_key=nonce
                    )
                except ValueError:
                    return None

        key_a, key_b = new_shop_request_key(), new_shop_request_key()
        results = await asyncio.gather(attempt(key_a), attempt(key_b))
        won = [row for row in results if row is not None]
        self.assertEqual(len(won), 1)
        day = await self.session.scalar(
            select(ShopBroadCampaignDay).where(ShopBroadCampaignDay.shop_id == SHOP_A)
        )
        self.assertEqual(int(day.used), 1)

    async def test_same_request_key_returns_the_same_queue_row(self):
        key = new_shop_request_key()
        first = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="once", created_by="a", request_key=key
        )
        second = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="once", created_by="a", request_key=key
        )
        self.assertEqual(first.id, second.id)
        rows = list(
            (
                await self.session.scalars(select(BroadcastLog).where(BroadcastLog.shop_id == SHOP_A))
            ).all()
        )
        self.assertEqual(len(rows), 1)

    async def test_restart_does_not_message_a_user_twice(self):
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="hi", created_by="a", request_key=new_shop_request_key()
        )
        await self.session.execute(
            BroadcastLog.__table__.update()
            .where(BroadcastLog.id == log.id)
            .values(
                status="running",
                cursor_id=3,
                inflight_id=4,
                lock_token="dead",
                locked_until=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        await self.session.commit()
        await self.run_all(log.id)
        self.assertEqual(self.bot.sent.count(2002), 0)
        self.assertIn(2003, self.bot.sent)
        row = await self.session.get(BroadcastLog, log.id)
        await self.session.refresh(row)
        self.assertEqual(row.status, STATUS_DONE)
        self.assertEqual(row.review_count, 1)


class InputAndFailureTests(ShopBroadcastCase):
    async def test_raw_text_is_stored_and_escaped_only_at_send(self):
        with self.assertRaises(ValueError):
            clean_shop_broadcast_text("   ")
        with self.assertRaises(ValueError):
            clean_shop_broadcast_text("x" * 5000)
        raw = '<b>hi</b> & <a href="x">y</a>'
        cleaned = clean_shop_broadcast_text(raw)
        self.assertEqual(cleaned, raw)
        self.assertIn("<b>", cleaned)
        # Truncate first, then escape — never cut mid-entity the way escape-then-slice would.
        amp_heavy = "a" * 38 + "&x"
        snippet = shop_broadcast_html_snippet(amp_heavy, limit=40)
        self.assertEqual(snippet, html.escape(amp_heavy[:40], quote=False))
        self.assertNotEqual(snippet, html.escape(amp_heavy, quote=False)[:40])

        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text=raw, created_by="a", request_key=new_shop_request_key()
        )
        self.assertEqual(log.text, raw)
        await self.run_all(log.id)
        self.assertTrue(self.bot.bodies)
        self.assertEqual(self.bot.bodies[0], html.escape(raw, quote=False))
        self.assertNotIn("<b>", self.bot.bodies[0])

    async def test_platform_broadcast_does_not_reescape_html(self):
        body = "<b>owner</b>"
        log = await queue_broadcast(self.session, text=body, audience="users", created_by="owner")
        await self.run_all(log.id)
        self.assertEqual(self.bot.bodies[0], body)

    async def test_queue_rejects_shop_without_bot(self):
        profile = await self.session.scalar(
            select(ResellerProfile).where(ResellerProfile.user_id == SHOP_A)
        )
        profile.bot_token = None
        await self.session.commit()
        with self.assertRaises(ValueError) as ctx:
            await queue_shop_broadcast(
                self.session, shop_id=SHOP_A, text="no bot", created_by="a", request_key=new_shop_request_key()
            )
        self.assertEqual(str(ctx.exception), SHOP_NO_BOT_MESSAGE)

    async def test_worker_marks_failed_when_shop_bot_disappears(self):
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="later", created_by="a", request_key=new_shop_request_key()
        )
        with patch(
            "app.services.reseller_bots.open_notify_bot_for_reseller",
            return_value=(None, False),
        ):
            await run_broadcast_worker(self.Session, self.bot, budget_seconds=5)
        row = await self.session.get(BroadcastLog, log.id)
        await self.session.refresh(row)
        self.assertEqual(row.status, STATUS_FAILED)
        self.assertEqual(self.bot.sent, [])

    async def test_queue_broadcast_has_no_dead_shop_kwargs(self):
        params = inspect.signature(queue_broadcast).parameters
        self.assertNotIn("total_override", params)
        self.assertNotIn("skip_text_clean", params)
        self.assertNotIn("shop_id", params)
        self.assertLessEqual(len(params), 5)

    async def test_one_failed_send_does_not_stop_the_queue(self):
        self.bot.behaviour[2001] = TelegramForbiddenError(method=MagicMock(), message="blocked")
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="hi", created_by="a", request_key=new_shop_request_key()
        )
        await self.run_all(log.id)
        row = await self.session.get(BroadcastLog, log.id)
        await self.session.refresh(row)
        self.assertEqual(row.status, STATUS_DONE)
        self.assertEqual(row.fail_count, 1)
        self.assertEqual(row.ok_count, 2)

    async def test_retry_after_is_honoured(self):
        self.bot.behaviour[2001] = TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=30)
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="hi", created_by="a", request_key=new_shop_request_key()
        )
        async with self.Session() as session:
            self.assertTrue(await process_broadcast_batch(session, log.id, self.bot, delay=0))
        row = await self.session.get(BroadcastLog, log.id)
        await self.session.refresh(row)
        self.assertEqual(self.bot.sent, [])
        self.assertIsNone(row.inflight_id)
        self.assertGreater(
            row.locked_until.replace(tzinfo=timezone.utc),
            datetime.now(timezone.utc) + timedelta(seconds=20),
        )


class AuthzWebTests(ShopBroadcastCase):
    def test_shop_broadcast_scope_requires_broadcast_permission_and_shop(self):
        with self.assertRaises(HTTPException):
            shop_broadcast_scope({"role": "reseller", "permissions": ["campaigns"], "bot_user_id": SHOP_A})
        with self.assertRaises(HTTPException):
            shop_broadcast_scope({"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": SHOP_A})
        with self.assertRaises(HTTPException):
            shop_broadcast_scope({"role": "reseller", "permissions": ["broadcast"], "bot_user_id": None})
        with self.assertRaises(HTTPException):
            shop_broadcast_scope(
                {
                    "role": "admin",
                    "permissions": [],
                    "org_principal_id": 1,
                    "org_depth": 0,
                    "org_parent_id": None,
                    "org_status": "active",
                }
            )
        self.assertEqual(
            shop_broadcast_scope({"role": "reseller", "permissions": ["broadcast"], "bot_user_id": SHOP_A}),
            SHOP_A,
        )

    async def test_web_routes_refuse_cross_shop_cancel_and_delete(self):
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="x", created_by="a", request_key=new_shop_request_key()
        )
        app = FastAPI()

        async def db():
            yield self.session

        async def require_staff():
            return {"role": "reseller", "permissions": ["broadcast"], "bot_user_id": SHOP_B}

        register_shop_broadcast_pages(app, render=lambda *a, **k: None, require_staff=require_staff, get_db=db)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            cancel = await client.post(f"/shop-broadcast/history/{log.id}/cancel", follow_redirects=False)
            delete = await client.post(f"/shop-broadcast/history/{log.id}/delete", follow_redirects=False)
        self.assertIn("err=", cancel.headers["location"])
        self.assertIn("err=", delete.headers["location"])
        await self.session.refresh(await self.session.get(BroadcastLog, log.id))
        self.assertEqual((await self.session.get(BroadcastLog, log.id)).status, STATUS_QUEUED)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "broadcast_shop", ROOT / "alembic/versions/0095_broadcast_shop_id.py"
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, audience TEXT, text TEXT, "
                    "total INTEGER, ok_count INTEGER, fail_count INTEGER, status TEXT)"
                )
            )
            conn.execute(text("INSERT INTO broadcast_logs VALUES (1, 'all', 'old', 1, 1, 0, 'done')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                cols = {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")}
                self.assertIn("shop_id", cols)
                self.assertIn("request_key", cols)
                self.assertEqual(conn.execute(text("SELECT text FROM broadcast_logs")).scalar(), "old")
                migration.downgrade()
                cols = {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")}
                self.assertNotIn("shop_id", cols)
                self.assertEqual(conn.execute(text("SELECT text FROM broadcast_logs")).scalar(), "old")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
