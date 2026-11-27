"""Why group-message recipients were not reached, and skipping people a tenant's bot cannot message."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.broadcast_detail_pages import register_broadcast_detail_routes
from app.api.broadcast_pages import BroadcastPage
from app.db.models import Base, BotUser, BroadcastFailure, BroadcastLog, BroadcastUnreachable, ResellerProfile
from app.services.broadcast import delete_broadcast_log
from app.services.broadcast_failures import (
    KIND_BAD_MESSAGE,
    KIND_BLOCKED,
    KIND_DEACTIVATED,
    KIND_FORBIDDEN,
    KIND_NOT_STARTED,
    KIND_UNKNOWN,
    broadcast_report,
    failure_kind,
    mark_reachable,
)
from app.services.broadcast_queue import process_broadcast_batch, queue_broadcast
from app.services.shop_broadcast import count_shop_broadcast_targets, new_shop_request_key, queue_shop_broadcast
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
SHOP_A, SHOP_B = 20, 30
PLATFORM_TG = (1001, 1002, 1003, 1004)
SHOP_A_TG = (2001, 2002)


def forbidden(description: str) -> TelegramForbiddenError:
    return TelegramForbiddenError(method=MagicMock(), message=description)


def bad_request(description: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=description)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[int] = []
        self.behaviour: dict[int, Exception] = {}

    async def send_message(self, chat_id, body, parse_mode=None, reply_markup=None):
        action = self.behaviour.get(chat_id)
        if action is not None:
            raise action
        self.sent.append(chat_id)


class FailureKindTests(unittest.TestCase):
    def test_telegram_answers_map_to_reason_kinds(self):
        cases = {
            forbidden("Forbidden: bot was blocked by the user"): KIND_BLOCKED,
            forbidden("Forbidden: user is deactivated"): KIND_DEACTIVATED,
            forbidden("Forbidden: bot can't initiate conversation with a user"): KIND_NOT_STARTED,
            bad_request("Bad Request: chat not found"): KIND_NOT_STARTED,
            forbidden("Forbidden: something new"): KIND_FORBIDDEN,
            bad_request("Bad Request: can't parse entities"): KIND_BAD_MESSAGE,
        }
        for exc, kind in cases.items():
            with self.subTest(exc.message):
                self.assertEqual(failure_kind(exc), kind)


class FailureCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all([
            BotUser(id=SHOP_A, telegram_id=9020, referral_code="shopa", role="reseller"),
            BotUser(id=SHOP_B, telegram_id=9030, referral_code="shopb", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=n, telegram_id=tg, referral_code=f"p{n}", role="user") for n, tg in enumerate(PLATFORM_TG, start=1)
        ] + [
            BotUser(id=n, telegram_id=tg, referral_code=f"a{n}", role="user", reseller_id=SHOP_A)
            for n, tg in enumerate(SHOP_A_TG, start=11)
        ] + [
            ResellerProfile(user_id=SHOP_A, bot_token="enc-a", is_active=True),
            ResellerProfile(user_id=SHOP_B, bot_token="enc-b", is_active=True),
        ])
        await self.session.commit()
        self.bot = FakeBot()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def run_all(self, log_id: int) -> None:
        for _ in range(50):
            async with self.Session() as session:
                if not await process_broadcast_batch(session, log_id, self.bot, delay=0):
                    return

    async def platform_broadcast(self, body: str = "hello") -> BroadcastLog:
        log = await queue_broadcast(self.session, text=body, audience="users", created_by="owner")
        await self.run_all(log.id)
        return log

    async def kinds(self, log_id: int) -> dict[int, str]:
        rows = await self.session.execute(
            select(BroadcastFailure.bot_user_id, BroadcastFailure.kind).where(BroadcastFailure.broadcast_id == log_id)
        )
        return dict(rows.all())


class RecordingTests(FailureCase):
    async def test_each_recipient_not_reached_is_recorded_with_its_reason(self):
        self.bot.behaviour = {
            1001: forbidden("Forbidden: bot was blocked by the user"),
            1002: bad_request("Bad Request: can't parse entities"),
            1003: TimeoutError("lost response"),
        }
        log = await self.platform_broadcast()
        self.assertEqual(await self.kinds(log.id), {1: KIND_BLOCKED, 2: KIND_BAD_MESSAGE, 3: KIND_UNKNOWN})
        report = await broadcast_report(self.session, log.id, shop_id=None)
        self.assertEqual({r.kind.key: r.count for r in report.reasons}, {KIND_BLOCKED: 1, KIND_BAD_MESSAGE: 1, KIND_UNKNOWN: 1})
        self.assertEqual(report.unrecorded, 0)

    async def test_a_recipient_left_in_flight_by_a_dead_worker_is_recorded_as_unknown(self):
        log = await queue_broadcast(self.session, text="hi", audience="users", created_by="owner")
        await self.session.execute(
            BroadcastLog.__table__.update().where(BroadcastLog.id == log.id).values(inflight_id=2, cursor_id=1, ok_count=1)
        )
        await self.session.commit()
        await self.run_all(log.id)
        self.assertEqual(await self.kinds(log.id), {2: KIND_UNKNOWN})
        self.assertNotIn(1002, self.bot.sent)

    async def test_a_broadcast_sent_before_reasons_existed_reports_its_failures_as_unrecorded(self):
        self.session.add(BroadcastLog(id=500, audience="all", text="old", total=10, ok_count=7, fail_count=3, status="done"))
        await self.session.commit()
        report = await broadcast_report(self.session, 500, shop_id=None)
        self.assertEqual((report.reasons, report.unrecorded), ([], 3))

    async def test_deleting_a_broadcast_removes_its_reasons(self):
        self.bot.behaviour = {1001: bad_request("Bad Request: can't parse entities")}
        log = await self.platform_broadcast()
        self.assertTrue(await delete_broadcast_log(self.session, log.id, shop_id=None))
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(BroadcastFailure)), 0)


class UnreachableTests(FailureCase):
    async def test_people_who_blocked_the_bot_are_skipped_by_later_broadcasts(self):
        self.bot.behaviour = {
            1001: forbidden("Forbidden: bot was blocked by the user"),
            1002: forbidden("Forbidden: user is deactivated"),
            1003: bad_request("Bad Request: chat not found"),
        }
        first = await self.platform_broadcast()
        self.assertEqual(first.total, 6)
        self.bot.behaviour, self.bot.sent = {}, []
        second = await self.platform_broadcast()
        self.assertEqual(second.total, 3)
        self.assertEqual(sorted(self.bot.sent), [1004, 9020, 9030])

    async def test_a_rejected_message_does_not_mark_anyone_unreachable(self):
        self.bot.behaviour = {1001: bad_request("Bad Request: can't parse entities"), 1002: TimeoutError("lost")}
        await self.platform_broadcast()
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(BroadcastUnreachable)), 0)

    async def test_starting_the_bot_again_includes_the_person_once_more(self):
        self.bot.behaviour = {1001: forbidden("Forbidden: bot was blocked by the user")}
        await self.platform_broadcast()
        await mark_reachable(self.session, user_id=1, shop_id=None)
        self.bot.behaviour, self.bot.sent = {}, []
        await self.platform_broadcast()
        self.assertIn(1001, self.bot.sent)

    async def test_blocking_one_bot_does_not_skip_the_person_in_another_tenants_bot(self):
        self.session.add(BroadcastUnreachable(user_id=11, shop_key=0, reason=KIND_BLOCKED))
        self.session.add(BroadcastUnreachable(user_id=12, shop_key=SHOP_B, reason=KIND_BLOCKED))
        await self.session.commit()
        self.assertEqual(await count_shop_broadcast_targets(self.session, shop_id=SHOP_A), 2)
        self.session.add(BroadcastUnreachable(user_id=11, shop_key=SHOP_A, reason=KIND_BLOCKED))
        await self.session.commit()
        self.assertEqual(await count_shop_broadcast_targets(self.session, shop_id=SHOP_A), 1)

    async def test_a_shop_broadcast_marks_the_person_only_for_that_shops_bot(self):
        self.bot.behaviour = {2001: forbidden("Forbidden: bot was blocked by the user")}
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="hi", created_by="a", request_key=new_shop_request_key()
        )
        await self.run_all(log.id)
        marks = (await self.session.execute(select(BroadcastUnreachable.user_id, BroadcastUnreachable.shop_key))).all()
        self.assertEqual(marks, [(11, SHOP_A)])

    async def test_start_command_clears_the_mark_for_the_bot_it_was_sent_to(self):
        from app.bot.handlers.start import cmd_start

        self.session.add_all([
            BroadcastUnreachable(user_id=11, shop_key=SHOP_A, reason=KIND_BLOCKED),
            BroadcastUnreachable(user_id=11, shop_key=0, reason=KIND_BLOCKED),
        ])
        await self.session.commit()
        user = await self.session.get(BotUser, 11)
        stop = RuntimeError("stop after the mark is cleared")
        with patch("app.bot.handlers.start.get_all_settings", AsyncMock(side_effect=stop)), self.assertRaises(RuntimeError):
            await cmd_start(
                MagicMock(), NS(args=""), self.session, user, AsyncMock(),
                is_reseller_bot=True, reseller_owner_id=SHOP_A,
            )
        marks = (await self.session.execute(select(BroadcastUnreachable.shop_key))).scalars().all()
        self.assertEqual(marks, [0])


class DetailPageTests(FailureCase):
    def render(self, request, name, context):
        from app.api.app import templates

        fake = NS(url=NS(path=context["page_path"]), query_params={}, state=NS(), cookies={}, headers={})
        body = templates.env.get_template(name).render(request=fake, csp_nonce="n", csrf_token="TOK", **context)
        return HTMLResponse(body)

    async def get(self, page: BroadcastPage, log_id: int) -> httpx.Response:
        app = FastAPI()

        async def db():
            yield self.session

        async def guard():
            return {"role": "admin"}

        register_broadcast_detail_routes(
            app, path=page.path, resolve=lambda _staff: page, guard=guard, render=self.render, get_db=db
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(f"{page.path}/history/{log_id}")

    async def test_the_page_lists_reasons_and_escapes_the_message(self):
        self.bot.behaviour = {1001: forbidden("Forbidden: bot was blocked by the user"), 1002: TimeoutError("x")}
        log = await self.platform_broadcast("<script>alert(1)</script>")
        response = await self.get(BroadcastPage(None, "/broadcast"), log.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("ربات را بلاک کرده‌اند", response.text)
        self.assertIn("معلوم نیست پیام رسیده یا نه", response.text)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertNotIn("blocked by the user", response.text)

    async def test_a_shop_cannot_open_the_platforms_or_another_shops_broadcast(self):
        platform = await self.platform_broadcast()
        shop_b = BroadcastLog(audience="all", text="b", total=1, status="done", shop_id=SHOP_B)
        self.session.add(shop_b)
        await self.session.commit()
        shop_a_page = BroadcastPage(SHOP_A, "/shop-broadcast")
        self.assertEqual((await self.get(shop_a_page, platform.id)).status_code, 404)
        self.assertEqual((await self.get(shop_a_page, shop_b.id)).status_code, 404)
        self.assertEqual((await self.get(BroadcastPage(None, "/broadcast"), shop_b.id)).status_code, 404)
        self.assertEqual((await self.get(BroadcastPage(SHOP_B, "/shop-broadcast"), shop_b.id)).status_code, 200)


class BroadcastFailuresMigrationTests(unittest.TestCase):
    def test_upgrade_twice_then_downgrade_keeps_broadcasts(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("m0133", ROOT / "alembic/versions/0133_broadcast_failures.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, text TEXT)"))
            conn.execute(text("INSERT INTO broadcast_logs VALUES (1, 'kept')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                tables = set(sa.inspect(conn).get_table_names())
                self.assertTrue({"broadcast_failures", "broadcast_unreachable"} <= tables)
                migration.downgrade()
                self.assertNotIn("broadcast_failures", set(sa.inspect(conn).get_table_names()))
            self.assertEqual(conn.execute(text("SELECT text FROM broadcast_logs")).scalar_one(), "kept")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
