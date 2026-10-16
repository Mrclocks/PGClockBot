"""Admins hear about a new «افزودن سرویس قبلی» request once, and only the right admins."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.exceptions import TelegramAPIError

from app.bot.service_import_ui import announce_new_request
from app.db.models import BotUser
from app.services.service_import import import_existing_service
from app.services.service_import_notify import NOTIFY_KEY
from app.services.users import clear_settings_cache, set_settings_bulk
from tests.test_service_import_approval import APPROVAL, ApprovalCase

PLATFORM_ADMINS = [7001, 7002]
SHOP_STAFF = [9001]


class NotifyCase(ApprovalCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.send = AsyncMock(return_value=1)
        shop_bot = (MagicMock(session=MagicMock(close=AsyncMock())), True)
        patches = (
            patch("app.services.notifications._send_to_chats", self.send),
            patch("app.services.notifications.get_settings", return_value=SimpleNamespace(admin_ids=PLATFORM_ADMINS)),
            patch("app.services.notifications._shop_recipient_chat_ids", AsyncMock(return_value=SHOP_STAFF)),
            patch("app.services.resellers.get_reseller_profile", AsyncMock(return_value=MagicMock(bot_token="shop:token"))),
            patch("app.services.reseller_bots.open_notify_bot_for_reseller", AsyncMock(return_value=shop_bot)),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def ask(self, user_id: int, link: str, *, shop_id: int | None = None):
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            result = await import_existing_service(session, user=user, raw_link=link, shop_id=shop_id)
            # The notice follows the bot the customer used, which the dispatcher sets per update.
            with patch("app.bot.service_import_ui.current_shop_reseller_id", return_value=shop_id):
                await announce_new_request(SimpleNamespace(bot=MagicMock()), session, user, result)
        return result


class NotifyTests(NotifyCase):
    async def test_a_platform_request_reaches_the_platform_admins_only(self):
        await self.ask(1, "tok-plat-0000001")
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.send.await_args.args[1], PLATFORM_ADMINS)
        self.assertIn("plat-one", self.send.await_args.args[2])

    async def test_a_shop_request_reaches_that_shops_staff_never_the_platform_admins(self):
        await self.ask(6, "tok-shop-0000001", shop_id=5)
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.send.await_args.args[1], SHOP_STAFF)

    async def test_repeating_the_same_link_does_not_notify_again(self):
        await self.ask(1, "tok-plat-0000001")
        await self.ask(1, "https://p.example/sub/tok-plat-0000001")
        self.assertEqual(self.send.await_count, 1)

    async def test_instant_mode_sends_nothing(self):
        async with self.Session() as session:
            await set_settings_bulk(session, {**APPROVAL, "service_import_mode": "instant"})
        clear_settings_cache()
        await self.ask(1, "tok-plat-0000001")
        self.send.assert_not_awaited()

    async def test_a_refused_link_sends_nothing(self):
        with self.assertRaises(Exception):
            await self.ask(1, "tok-shop-0000001")
        self.send.assert_not_awaited()

    async def test_hostile_text_is_escaped(self):
        self.platform_users["tok-plat-0000001"]["username"] = "<b>x</b>&"
        await self.ask(1, "tok-plat-0000001")
        text = self.send.await_args.args[2]
        self.assertIn("&lt;b&gt;x&lt;/b&gt;&amp;", text)
        self.assertNotIn("<b>x</b>&", text)

    async def test_a_telegram_failure_never_breaks_the_customers_request(self):
        with patch("app.services.service_import_notify._dispatch_dual_notify", AsyncMock(side_effect=TelegramAPIError(MagicMock(), "down"))):
            result = await self.ask(1, "tok-plat-0000001")
        self.assertEqual(result.request_id is not None, True)
        self.assertEqual(len(await self.requests()), 1)

    async def test_the_shop_can_switch_the_notice_off(self):
        with patch("app.services.notifications.notify_enabled", AsyncMock(return_value=False)) as enabled:
            await self.ask(6, "tok-shop-0000001", shop_id=5)
        enabled.assert_awaited()
        self.assertEqual(enabled.await_args.args[1], NOTIFY_KEY)
        self.send.assert_not_awaited()
