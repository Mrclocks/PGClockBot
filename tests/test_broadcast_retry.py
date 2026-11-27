"""Resending a finished group message to the recipients it did not reach."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.broadcast_detail_pages import register_broadcast_detail_routes
from app.api.broadcast_pages import BroadcastPage
from app.db.models import BroadcastLog, BroadcastUnreachable
from app.services.broadcast_queue import STATUS_DONE, STATUS_QUEUED
from app.services.broadcast_retry import queue_retry, retry_offer
from app.services.shop_broadcast import new_shop_request_key, queue_shop_broadcast
from tests.postgres_support import scoped_sync_engine
from tests.test_broadcast_failures import SHOP_A, SHOP_B, FailureCase, bad_request, forbidden

ROOT = Path(__file__).resolve().parents[1]


class RetryCase(FailureCase):
    async def failed_broadcast(self) -> BroadcastLog:
        """Platform broadcast: 1001 blocked the bot, 1002 rejected, 1003 unknown, the rest delivered."""
        self.bot.behaviour = {
            1001: forbidden("Forbidden: bot was blocked by the user"),
            1002: bad_request("Bad Request: can't parse entities"),
            1003: TimeoutError("lost response"),
        }
        log = await self.platform_broadcast()
        await self.session.refresh(log)
        self.bot.behaviour, self.bot.sent = {}, []
        return log

    async def resend(self, log_id: int, *, shop_id=None, include_unknown=False) -> BroadcastLog:
        return await queue_retry(
            self.session, log_id, shop_id=shop_id, include_unknown=include_unknown, created_by="owner"
        )


class QueueRetryTests(RetryCase):
    async def test_the_resend_reaches_only_people_who_did_not_get_the_message_and_can_get_it_now(self):
        log = await self.failed_broadcast()
        resend = await self.resend(log.id)
        self.assertEqual((resend.total, resend.status, resend.text), (1, STATUS_QUEUED, "hello"))
        await self.run_all(resend.id)
        self.assertEqual(self.bot.sent, [1002])

    async def test_someone_who_started_the_bot_again_is_included(self):
        log = await self.failed_broadcast()
        await self.session.execute(delete(BroadcastUnreachable).where(BroadcastUnreachable.user_id == 1))
        await self.session.commit()
        resend = await self.resend(log.id)
        await self.run_all(resend.id)
        self.assertEqual(sorted(self.bot.sent), [1001, 1002])

    async def test_unknown_outcomes_are_resent_only_when_asked(self):
        log = await self.failed_broadcast()
        offer = await retry_offer(self.session, log)
        self.assertEqual((offer.failed, offer.with_unknown, offer.existing_id), (1, 2, None))
        resend = await self.resend(log.id, include_unknown=True)
        await self.run_all(resend.id)
        self.assertEqual(sorted(self.bot.sent), [1002, 1003])

    async def test_a_broadcast_is_resent_once_and_a_repeat_returns_that_resend(self):
        log = await self.failed_broadcast()
        first = await self.resend(log.id)
        self.assertEqual((await self.resend(log.id, include_unknown=True)).id, first.id)
        self.assertEqual((await retry_offer(self.session, log)).existing_id, first.id)
        self.session.add(BroadcastLog(audience="all", text="x", total=1, status=STATUS_DONE, retry_of_id=log.id))
        with self.assertRaises(IntegrityError):
            await self.session.commit()

    async def test_an_active_or_fully_delivered_broadcast_cannot_be_resent(self):
        sending = await queue_broadcast_row(self.session)
        with self.assertRaises(ValueError):
            await self.resend(sending.id)
        self.assertIsNone(await retry_offer(self.session, sending))
        delivered = await self.platform_broadcast()
        await self.session.refresh(delivered)
        with self.assertRaises(ValueError):
            await self.resend(delivered.id)

    async def test_deleting_the_original_never_widens_a_queued_resend(self):
        log = await self.failed_broadcast()
        resend = await self.resend(log.id)
        await self.session.execute(delete(BroadcastLog).where(BroadcastLog.id == log.id))
        await self.session.commit()
        await self.run_all(resend.id)
        self.assertEqual(self.bot.sent, [])


class RetryTenantTests(RetryCase):
    async def shop_failed_broadcast(self) -> BroadcastLog:
        self.bot.behaviour = {2001: bad_request("Bad Request: can't parse entities")}
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="<b>hi</b>", created_by="a", request_key=new_shop_request_key()
        )
        await self.run_all(log.id)
        await self.session.refresh(log)
        self.bot.behaviour, self.bot.sent = {}, []
        return log

    async def test_a_shop_resends_only_its_own_broadcast_to_its_own_customers(self):
        log = await self.shop_failed_broadcast()
        for shop_id in (None, SHOP_B):
            with self.subTest(shop_id=shop_id), self.assertRaises(ValueError):
                await self.resend(log.id, shop_id=shop_id)
        platform = await self.failed_broadcast()
        with self.assertRaises(ValueError):
            await self.resend(platform.id, shop_id=SHOP_A)
        resend = await self.resend(log.id, shop_id=SHOP_A)
        await self.run_all(resend.id)
        self.assertEqual(self.bot.sent, [2001])

    async def test_a_shop_resend_does_not_use_the_daily_broad_message(self):
        log_id = (await self.shop_failed_broadcast()).id
        with self.assertRaises(ValueError):
            await queue_shop_broadcast(self.session, shop_id=SHOP_A, text="again", created_by="a",
                                       request_key=new_shop_request_key())
        await self.session.rollback()
        self.assertEqual((await self.resend(log_id, shop_id=SHOP_A)).total, 1)

    async def test_the_route_queues_for_the_session_tenant_only(self):
        log_id = (await self.shop_failed_broadcast()).id

        async def post(page: BroadcastPage) -> str:
            app = FastAPI()

            async def db():
                yield self.session

            async def guard():
                return {"username": "staff"}

            register_broadcast_detail_routes(
                app, path=page.path, resolve=lambda _staff: page, guard=guard,
                render=lambda *_a: HTMLResponse(""), get_db=db,
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(f"{page.path}/history/{log_id}/retry", data={"include_unknown": "1"})
            return response.headers["location"]

        self.assertIn("err=", await post(BroadcastPage(SHOP_B, "/shop-broadcast")))
        self.assertIn("ok=", await post(BroadcastPage(SHOP_A, "/shop-broadcast")))
        resend = await self.session.scalar(select(BroadcastLog).where(BroadcastLog.retry_of_id == log_id))
        self.assertEqual((resend.shop_id, resend.retry_unknown, resend.created_by), (SHOP_A, True, "staff"))
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(BroadcastLog)), 2)


class RetryPageTests(RetryCase):
    def render(self, request, name, context):
        from types import SimpleNamespace as NS

        from app.api.app import templates

        fake = NS(url=NS(path=context["page_path"]), query_params={}, state=NS(), cookies={}, headers={})
        return HTMLResponse(templates.env.get_template(name).render(request=fake, csp_nonce="n", csrf_token="TOK", **context))

    async def page(self, log_id: int) -> str:
        app = FastAPI()

        async def db():
            yield self.session

        async def guard():
            return {"role": "admin"}

        register_broadcast_detail_routes(
            app, path="/broadcast", resolve=lambda _s: BroadcastPage(None, "/broadcast"), guard=guard,
            render=self.render, get_db=db,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return (await client.get(f"/broadcast/history/{log_id}")).text

    async def test_the_detail_page_offers_the_resend_then_links_to_it(self):
        log = await self.failed_broadcast()
        html = await self.page(log.id)
        self.assertIn(f'action="/broadcast/history/{log.id}/retry"', html)
        self.assertIn('name="include_unknown"', html)
        self.assertIn('value="TOK"', html)
        resend = await self.resend(log.id)
        html = await self.page(log.id)
        self.assertNotIn(f'action="/broadcast/history/{log.id}/retry"', html)
        self.assertIn(f'href="/broadcast/history/{resend.id}"', html)


async def queue_broadcast_row(session) -> BroadcastLog:
    from app.services.broadcast_queue import queue_broadcast

    return await queue_broadcast(session, text="still sending", audience="users", created_by="owner")


class BroadcastRetryMigrationTests(unittest.TestCase):
    def test_upgrade_twice_then_downgrade_keeps_broadcasts(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("m0134", ROOT / "alembic/versions/0134_broadcast_retry.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, text TEXT)"))
            conn.execute(text("INSERT INTO broadcast_logs VALUES (1, 'kept')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(
                    conn.execute(text("SELECT retry_of_id, retry_unknown FROM broadcast_logs")).one(), (None, False)
                )
                migration.downgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")}
                self.assertNotIn("retry_of_id", columns)
            self.assertEqual(conn.execute(text("SELECT text FROM broadcast_logs")).scalar_one(), "kept")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
