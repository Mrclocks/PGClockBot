"""Mini App support tickets: customer and shop isolation, limits, staff alerts."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_support import register_miniapp_support
from app.db.models import Base, BotUser, Ticket, TicketMessage
from app.services.customer_support import (
    BODY_MAX,
    MESSAGE_RATE_LIMIT,
    OPEN_TICKET_LIMIT,
    SUBJECT_MAX,
    THREAD_MESSAGE_LIMIT,
    support_overview,
)
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

HOSTILE = "<b onmouseover=alert(1)>hi</b>"


class MiniAppSupportTestCase(unittest.IsolatedAsyncioTestCase):
    caller_id = 1

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="a", role="user", full_name="Ali"),
            BotUser(id=2, telegram_id=102, referral_code="b", role="user"),
            BotUser(id=4, telegram_id=104, referral_code="c", role="admin"),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        self.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        self.notify_new = AsyncMock(return_value=1)
        self.notify_message = AsyncMock()

        async def load_user(*_args):
            return await self.session.scalar(select(BotUser).where(BotUser.id == self.caller_id))

        self.patches = [
            patch("app.api.miniapp_support.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_pages.get_all_settings", AsyncMock(return_value={})),
            patch("app.api.miniapp_support.open_notify_bot_for_user", AsyncMock(return_value=(self.bot, True))),
            patch("app.services.customer_support.notify_new_ticket", self.notify_new),
            patch("app.services.customer_support.notify_ticket_message", self.notify_message),
            patch("app.services.miniapp_auth.is_bot_platform_admin", side_effect=lambda u: u.role == "admin"),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()

        async def get_db():
            yield self.session

        register_miniapp_support(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def open_ticket(self, subject="vpn down", body="it does not connect"):
        return await self.client.post("/api/mini/support/tickets", json={"subject": subject, "body": body})

    async def count(self, model) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(model)) or 0)


class SupportTicketTests(MiniAppSupportTestCase):
    async def test_open_reply_and_read_a_platform_ticket(self) -> None:
        opened = await self.open_ticket(subject="  " + HOSTILE + "  ", body=HOSTILE)
        self.assertEqual(opened.status_code, 200, opened.text)
        ticket_id = opened.json()["ticket_id"]
        ticket = await self.session.get(Ticket, ticket_id)
        self.assertEqual((ticket.user_id, ticket.reseller_id, ticket.subject), (1, None, HOSTILE))
        self.notify_new.assert_awaited_once()
        self.assertEqual(self.notify_new.await_args.kwargs["ticket_reseller_id"], None)
        self.bot.session.close.assert_awaited()

        reply = await self.client.post(f"/api/mini/support/tickets/{ticket_id}/messages", json={"body": "any news?"})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.notify_message.assert_awaited_once()
        self.session.add(TicketMessage(ticket_id=ticket_id, sender_id=999, is_staff=True, body="fixed"))
        await self.session.commit()

        thread = (await self.client.get(f"/api/mini/support/tickets/{ticket_id}")).json()["ticket"]
        self.assertEqual([(m["from_staff"], m["body"]) for m in thread["messages"]],
                         [(False, HOSTILE), (False, "any news?"), (True, "fixed")])
        listed = (await self.client.get("/api/mini/support")).json()["tickets"]
        self.assertEqual([t["id"] for t in listed], [ticket_id])

    async def test_other_customers_and_shop_tickets_are_invisible(self) -> None:
        self.session.add_all([
            Ticket(id=10, user_id=2, subject="other customer", status="open"),
            Ticket(id=11, user_id=1, subject="shop ticket", status="open", reseller_id=3),
        ])
        await self.session.commit()
        for ticket_id in (10, 11, 999, 0):
            with self.subTest(ticket_id=ticket_id):
                self.assertEqual((await self.client.get(f"/api/mini/support/tickets/{ticket_id}")).status_code, 404)
                reply = await self.client.post(f"/api/mini/support/tickets/{ticket_id}/messages", json={"body": "x"})
                self.assertIn(reply.status_code, (400, 404))
        listed = (await self.client.get("/api/mini/support")).json()["tickets"]
        self.assertEqual(listed, [])
        self.assertEqual(await self.count(TicketMessage), 0)
        self.notify_message.assert_not_awaited()

    async def test_closed_ticket_cannot_be_reopened_by_a_reply(self) -> None:
        self.session.add(Ticket(id=12, user_id=1, subject="old", status="closed"))
        await self.session.commit()
        reply = await self.client.post("/api/mini/support/tickets/12/messages", json={"body": "hello"})
        self.assertEqual(reply.status_code, 400)
        self.assertEqual((await self.session.get(Ticket, 12)).status, "closed")
        self.assertEqual(await self.count(TicketMessage), 0)

    async def test_rejects_empty_oversized_and_non_text_input(self) -> None:
        bad = [
            {"subject": "", "body": "x"}, {"subject": "   ", "body": "x"}, {"subject": "x", "body": ""},
            {"subject": "x" * (SUBJECT_MAX + 1), "body": "x"}, {"subject": "x", "body": "y" * (BODY_MAX + 1)},
            {"subject": 5, "body": "x"}, {"subject": "x", "body": ["y"]}, {"subject": None, "body": None},
        ]
        for payload in bad:
            with self.subTest(payload=str(payload)[:60]):
                self.assertEqual((await self.client.post("/api/mini/support/tickets", json=payload)).status_code, 400)
        self.assertEqual((await self.client.post("/api/mini/support/tickets", json=["x"])).status_code, 400)
        self.assertEqual(await self.count(Ticket), 0)
        self.notify_new.assert_not_awaited()

    async def test_open_ticket_and_message_rate_limits(self) -> None:
        for i in range(OPEN_TICKET_LIMIT):
            self.assertEqual((await self.open_ticket(subject=f"t{i}")).status_code, 200)
        self.assertEqual((await self.open_ticket()).status_code, 400)
        ticket_id = (await self.client.get("/api/mini/support")).json()["tickets"][0]["id"]
        for _ in range(MESSAGE_RATE_LIMIT - OPEN_TICKET_LIMIT):
            self.assertEqual((await self.client.post(f"/api/mini/support/tickets/{ticket_id}/messages", json={"body": "x"})).status_code, 200)
        self.assertEqual((await self.client.post(f"/api/mini/support/tickets/{ticket_id}/messages", json={"body": "x"})).status_code, 400)
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        for message in await self.session.scalars(select(TicketMessage)):
            message.created_at = old
        await self.session.commit()
        self.assertEqual((await self.client.post(f"/api/mini/support/tickets/{ticket_id}/messages", json={"body": "x"})).status_code, 200)

    async def test_thread_is_bounded_to_the_latest_messages(self) -> None:
        self.session.add(Ticket(id=20, user_id=1, subject="long", status="open"))
        self.session.add_all([TicketMessage(ticket_id=20, sender_id=101, body=f"m{i}") for i in range(THREAD_MESSAGE_LIMIT + 5)])
        await self.session.commit()
        messages = (await self.client.get("/api/mini/support/tickets/20")).json()["ticket"]["messages"]
        self.assertEqual(len(messages), THREAD_MESSAGE_LIMIT)
        self.assertEqual(messages[-1]["body"], f"m{THREAD_MESSAGE_LIMIT + 4}")

    async def test_failed_staff_alert_keeps_the_ticket(self) -> None:
        self.notify_new.side_effect = RuntimeError("telegram down")
        response = await self.open_ticket()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(await self.count(Ticket), 1)

    async def test_admin_has_no_customer_support_screen(self) -> None:
        self.caller_id = 4
        self.assertEqual((await self.client.get("/api/mini/support")).status_code, 403)
        self.assertEqual((await self.open_ticket()).status_code, 403)
        self.assertEqual(await self.count(Ticket), 0)


class SupportOverviewTests(unittest.TestCase):
    def test_only_public_telegram_links_are_offered(self) -> None:
        contacts = json.dumps([
            {"title": "Main", "telegram": "@support_team"},
            {"title": "By id", "telegram": "123456"},
            {"title": "Off", "telegram": "@off", "enabled": False},
        ])
        overview = support_overview({"support_contacts": contacts, "support_text": "<b>سلام</b>"})
        self.assertEqual(overview["contacts"], [{"title": "Main", "url": "https://t.me/support_team"}])
        self.assertNotIn("<b>", overview["text"])


if __name__ == "__main__":
    unittest.main()
