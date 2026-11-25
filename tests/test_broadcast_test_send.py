"""Test send of a composed group message to the sender's own account through the tenant's bot."""

from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import httpx
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.broadcast_pages import BroadcastPage
from app.api.broadcast_test_pages import register_broadcast_test_routes
from app.db.models import Base, BotUser, BroadcastLog
from app.services.broadcast_test_send import (
    NO_RECIPIENT,
    NOT_REACHABLE,
    REJECTED,
    send_test_broadcast,
)
from app.services.message_buttons import ButtonInput, broadcast_buttons
from app.services.shop_broadcast import SHOP_NO_BOT_MESSAGE
from tests.postgres_support import scoped_async_engine

SHOP_A, SHOP_B = 20, 30
ADMINS = [7001, 7002]


class FakeBot:
    def __init__(self, behaviour: dict | None = None) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.behaviour = behaviour or {}

    async def send_message(self, chat_id, body, parse_mode=None, reply_markup=None, request_timeout=None):
        if chat_id in self.behaviour:
            raise self.behaviour[chat_id]
        self.sent.append((chat_id, body, reply_markup))


def opener(bot):
    @asynccontextmanager
    async def open_bot(_session, _shop_id):
        yield bot

    return open_bot


class TestSendCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=SHOP_A, telegram_id=9020, referral_code="shopa", role="reseller"),
            BotUser(id=SHOP_B, telegram_id=9030, referral_code="shopb", role="reseller"),
        ])
        await self.session.commit()
        settings = patch("app.services.broadcast_test_send.get_settings", return_value=NS(admin_ids=ADMINS))
        settings.start()
        self.addCleanup(settings.stop)

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def send(self, bot, *, shop_id=None, text="hello", buttons=None) -> int:
        return await send_test_broadcast(self.session, shop_id=shop_id, text=text, buttons=buttons, open_bot=opener(bot))


class SendTests(TestSendCase):
    async def test_the_platform_test_goes_to_the_bot_admins_as_the_worker_would_send_it(self):
        bot = FakeBot()
        self.assertEqual(await self.send(bot, text="<b>hi</b> 1 < 2"), 2)
        self.assertEqual([chat for chat, _body, _markup in bot.sent], ADMINS)
        self.assertEqual(bot.sent[0][1], "<b>hi</b> 1 &lt; 2")
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(BroadcastLog)), 0)

    async def test_a_shop_test_goes_only_to_that_shop_owner_with_its_text_escaped(self):
        bot = FakeBot()
        self.assertEqual(await self.send(bot, shop_id=SHOP_A, text="<b>hi</b>"), 1)
        self.assertEqual(bot.sent, [(9020, "&lt;b&gt;hi&lt;/b&gt;", None)])

    async def test_buttons_are_sent_under_the_message(self):
        bot = FakeBot()
        buttons = await broadcast_buttons(self.session, [ButtonInput("support", "پشتیبانی")], shop_id=None)
        await self.send(bot, buttons=buttons)
        markup = bot.sent[0][2]
        self.assertEqual([button.text for row in markup.inline_keyboard for button in row], ["پشتیبانی"])

    async def test_a_rejected_message_explains_what_to_check(self):
        bot = FakeBot({chat: TelegramBadRequest(method=MagicMock(), message="Bad Request: can't parse entities") for chat in ADMINS})
        with self.assertRaisesRegex(ValueError, REJECTED):
            await self.send(bot)

    async def test_an_account_that_never_started_the_bot_is_told_to_start_it(self):
        bot = FakeBot({9020: TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")})
        with self.assertRaisesRegex(ValueError, NOT_REACHABLE):
            await self.send(bot, shop_id=SHOP_A)

    async def test_one_reachable_admin_is_enough(self):
        bot = FakeBot({7001: TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")})
        self.assertEqual(await self.send(bot), 1)

    async def test_invalid_text_missing_bot_or_no_admin_ids_send_nothing(self):
        bot = FakeBot()
        with self.assertRaises(ValueError):
            await self.send(bot, text="   ")
        with self.assertRaisesRegex(ValueError, SHOP_NO_BOT_MESSAGE):
            await self.send(None, shop_id=SHOP_A)
        with patch("app.services.broadcast_test_send.get_settings", return_value=NS(admin_ids=[])):
            with self.assertRaisesRegex(ValueError, NO_RECIPIENT):
                await self.send(bot)
        self.assertEqual(bot.sent, [])


class RouteTests(TestSendCase):
    async def post(self, page: BroadcastPage, bot: FakeBot, data: dict) -> httpx.Response:
        app = FastAPI()

        async def db():
            yield self.session

        async def guard():
            return {"role": "reseller"}

        register_broadcast_test_routes(app, path=page.path, resolve=lambda _staff: page, guard=guard, get_db=db)
        with patch("app.services.broadcast_test_send.tenant_bot", opener(bot)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post(f"{page.path}/test", data=data)

    async def test_the_route_sends_through_the_session_tenant_and_answers_json(self):
        bot = FakeBot()
        response = await self.post(BroadcastPage(SHOP_B, "/shop-broadcast"), bot, {"text": "hi", "shop_id": str(SHOP_A)})
        self.assertEqual(response.status_code, 200)
        self.assertIn("message", response.json())
        self.assertEqual([chat for chat, _b, _m in bot.sent], [9030])

    async def test_a_refusal_is_a_safe_json_error(self):
        bot = FakeBot({9020: TelegramBadRequest(method=MagicMock(), message="Bad Request: secret detail")})
        response = await self.post(BroadcastPage(SHOP_A, "/shop-broadcast"), bot, {"text": "hi"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], REJECTED)
        self.assertNotIn("secret detail", response.text)


if __name__ == "__main__":
    unittest.main()
