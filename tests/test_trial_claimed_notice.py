"""Staff hear about each delivered free trial, with the customer's username, unless switched off."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import shop
from app.db.models import Base, BotUser, Order, OrderStatus, Plan, ResellerProfile
from app.services.notifications import PLATFORM_NOTIFY_PREFS, shop_notify_catalog
from app.services.trial_notify import NOTIFY_KEY, notify_trial_claimed, notify_trial_claimed_outside_bot
from app.services.users import clear_settings_cache, set_settings_bulk
from tests.test_trial_claim_unknown_delivery import BUYER, TRIAL_PLAN, TrialDeliveryTestCase

PLATFORM_ADMINS = [7001, 7002]
SHOP_STAFF = [9001]
SHOP = 5
PAID_PLAN = 2
SHOP_TRIAL_PLAN = 3


class TrialNoticeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.buyer = BotUser(
            id=1, telegram_id=6127326933, referral_code="b", role="user", full_name="Ali", username="ali_vpn"
        )
        self.session.add_all([
            self.buyer,
            BotUser(id=SHOP, telegram_id=55, referral_code="s", role="reseller"),
            ResellerProfile(user_id=SHOP, pg_admin_username="shopadmin", is_active=True),
            Plan(id=TRIAL_PLAN, name="تست ۱ روزه", price=0, is_active=True, is_trial=True),
            Plan(id=PAID_PLAN, name="paid", price=100, is_active=True),
            Plan(id=SHOP_TRIAL_PLAN, name="shop trial", price=0, is_active=True, is_trial=True, owner_reseller_id=SHOP),
        ])
        await self.session.commit()
        self.send = AsyncMock(return_value=1)
        shop_bot = (MagicMock(session=MagicMock(close=AsyncMock())), True)
        for p in (
            patch("app.services.notifications._send_to_chats", self.send),
            patch("app.services.notifications.get_settings", return_value=SimpleNamespace(admin_ids=PLATFORM_ADMINS)),
            patch("app.services.notifications._shop_recipient_chat_ids", AsyncMock(return_value=SHOP_STAFF)),
            patch("app.services.resellers.get_reseller_profile", AsyncMock(return_value=MagicMock(bot_token="s:t"))),
            patch("app.services.reseller_bots.open_notify_bot_for_reseller", AsyncMock(return_value=shop_bot)),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        clear_settings_cache()

    async def order(
        self, plan_id: int = TRIAL_PLAN, *, shop_id: int | None = None, status: str = OrderStatus.DELIVERED.value
    ) -> Order:
        row = Order(user_id=1, plan_id=plan_id, reseller_id=shop_id, amount=0, status=status)
        self.session.add(row)
        await self.session.commit()
        return row

    async def notify(self, order: Order) -> int:
        return await notify_trial_claimed(MagicMock(), self.session, order=order, user=self.buyer)


class TrialNoticeTests(TrialNoticeCase):
    async def test_platform_trial_tells_admins_the_username(self) -> None:
        await self.notify(await self.order())

        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.args[1], PLATFORM_ADMINS)
        text = self.send.await_args.args[2]
        self.assertIn("@ali_vpn", text)
        self.assertIn("6127326933", text)
        self.assertIn("تست ۱ روزه", text)

    async def test_switched_off_sends_nothing(self) -> None:
        await set_settings_bulk(self.session, {NOTIFY_KEY: "0"}, reseller_id=0)
        clear_settings_cache()
        self.assertEqual(await self.notify(await self.order()), 0)
        self.send.assert_not_awaited()

    async def test_shop_trial_reaches_that_shops_staff_never_platform_admins(self) -> None:
        await self.notify(await self.order(SHOP_TRIAL_PLAN, shop_id=SHOP))

        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.args[1], SHOP_STAFF)

    async def test_shop_switch_is_separate_from_the_platform_switch(self) -> None:
        await set_settings_bulk(self.session, {NOTIFY_KEY: "0"}, reseller_id=SHOP)
        clear_settings_cache()
        await self.notify(await self.order(SHOP_TRIAL_PLAN, shop_id=SHOP))
        self.send.assert_not_awaited()
        await self.notify(await self.order())
        self.assertEqual(self.send.await_args.args[1], PLATFORM_ADMINS)

    async def test_paid_or_undelivered_orders_send_nothing(self) -> None:
        await self.notify(await self.order(PAID_PLAN))
        await self.notify(await self.order(status=OrderStatus.PAID.value))
        self.send.assert_not_awaited()

    async def test_customer_without_username_and_hostile_name(self) -> None:
        self.buyer.username = None
        self.buyer.full_name = "<b>x</b>&"
        await self.notify(await self.order())

        text = self.send.await_args.args[2]
        self.assertIn("ندارد", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;&amp;", text)
        self.assertNotIn("<b>x</b>", text)

    async def test_telegram_failure_is_swallowed(self) -> None:
        with patch(
            "app.services.trial_notify._dispatch_dual_notify",
            AsyncMock(side_effect=TelegramAPIError(MagicMock(), "down")),
        ):
            self.assertEqual(await self.notify(await self.order()), 0)

    async def test_mini_app_path_opens_and_closes_its_bot(self) -> None:
        bot = MagicMock(session=MagicMock(close=AsyncMock()))
        with patch("app.services.reseller_bots.open_notify_bot_for_user", AsyncMock(return_value=(bot, True))):
            await notify_trial_claimed_outside_bot(self.session, order=await self.order(), user=self.buyer)
        self.send.assert_awaited_once()
        bot.session.close.assert_awaited_once()


class TrialNoticeSwitchTests(unittest.TestCase):
    def test_switch_listed_for_platform_and_for_shops_with_orders_permission(self) -> None:
        self.assertIn(NOTIFY_KEY, [key for key, *_ in PLATFORM_NOTIFY_PREFS])
        self.assertIn(NOTIFY_KEY, [key for key, *_ in shop_notify_catalog(["orders"])])
        self.assertNotIn(NOTIFY_KEY, [key for key, *_ in shop_notify_catalog(["tickets"])])


class ShopFreeDeliveryNoticeTests(TrialDeliveryTestCase):
    async def deliver(self) -> AsyncMock:
        from app.services.orders import create_order

        order = await create_order(self.session, user_id=BUYER, plan_id=TRIAL_PLAN)
        buyer = await self.session.get(BotUser, BUYER)
        notice = AsyncMock()
        with patch.object(shop, "notify_trial_claimed", notice), patch(
            "app.services.notifications.notify_approved_delivery_stuck", AsyncMock()
        ):
            await shop._deliver_free_order(MagicMock(), self.session, buyer, order)
        return notice

    async def test_delivered_trial_is_announced_once(self) -> None:
        self.pg.create_user_from_template.side_effect = None
        self.pg.create_user_from_template.return_value = {"id": 9, "username": "t1"}
        notice = await self.deliver()
        notice.assert_awaited_once()

    async def test_failed_trial_is_not_announced(self) -> None:
        self.panel_refuses()
        notice = await self.deliver()
        notice.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
