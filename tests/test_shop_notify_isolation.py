"""Events of a reseller shop reach only that shop's staff and bot, never the platform admins."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, Payment, PaymentStatus
from app.services import notifications
from app.services.notifications import (
    notify_account_edit,
    notify_stars_delivery_failed,
    notify_wallet_topup_ok,
)
from app.services.table_bulk import bulk_toggle_block

SHOP = 5
PLATFORM_ADMIN = 9001
SHOP_STAFF = 5005
CUSTOMER_TG = 1010


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.sent: list[tuple[int, str]] = []
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((int(chat_id), text))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=len(self.sent))


class ShopNotifyIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=SHOP, telegram_id=SHOP_STAFF, referral_code="o5", role="reseller"),
            BotUser(id=10, telegram_id=CUSTOMER_TG, referral_code="c10", role="user", reseller_id=SHOP),
            BotUser(id=11, telegram_id=1011, referral_code="c11", role="user"),
            Order(id=1, user_id=10, amount=5_000, reseller_id=SHOP),
            Payment(id=1, user_id=10, amount=5_000, method="stars", status=PaymentStatus.PENDING.value, order_id=1),
            Payment(id=2, user_id=10, amount=5_000, method="card", status=PaymentStatus.APPROVED.value,
                    is_wallet_topup=True, wallet_shop_id=SHOP),
            Payment(id=3, user_id=11, amount=5_000, method="card", status=PaymentStatus.APPROVED.value,
                    is_wallet_topup=True),
        ])
        await self.session.commit()
        self.platform_bot = FakeBot("111:platform")
        self.shop_bot = FakeBot("222:shop")
        settings = SimpleNamespace(admin_ids=[PLATFORM_ADMIN], currency="تومان", bot_token="111:platform")
        self.patches = [
            patch("app.services.notifications.get_settings", return_value=settings),
            patch("app.services.notifications.notify_enabled", AsyncMock(return_value=True)),
            patch("app.services.notifications._shop_recipient_chat_ids", AsyncMock(return_value=[SHOP_STAFF])),
            patch("app.services.reseller_bots.open_notify_bot_for_reseller",
                  AsyncMock(return_value=(self.shop_bot, False))),
            patch("app.services.resellers.get_reseller_profile", AsyncMock(return_value=None)),
            patch("app.bot.create_bot", return_value=self.platform_bot),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    def recipients(self) -> set[int]:
        return {chat for chat, _ in self.platform_bot.sent + self.shop_bot.sent}

    async def test_a_shop_wallet_top_up_is_not_reported_to_platform_admins(self) -> None:
        await notify_wallet_topup_ok(self.shop_bot, self.session, await self.session.get(Payment, 2), CUSTOMER_TG)
        self.assertEqual(self.recipients(), set())
        await notify_wallet_topup_ok(self.platform_bot, self.session, await self.session.get(Payment, 3), 1011)
        self.assertEqual(self.recipients(), {PLATFORM_ADMIN}, "a platform top-up is still announced")

    async def test_a_stars_failure_in_a_shop_reaches_only_that_shops_staff(self) -> None:
        await notify_stars_delivery_failed(
            self.shop_bot, self.session, await self.session.get(Payment, 1),
            telegram_id=CUSTOMER_TG, charge_id="ch<1>", refunded=False, error="<b>boom</b>",
        )
        self.assertEqual(self.recipients(), {SHOP_STAFF})
        text = self.shop_bot.sent[0][1]
        self.assertIn("&lt;b&gt;boom&lt;/b&gt;", text)
        self.assertIn("ch&lt;1&gt;", text)

    async def test_a_shop_account_edit_stays_in_the_shop(self) -> None:
        customer = await self.session.get(BotUser, 10)
        with patch("app.services.notifications._send_admins", AsyncMock()) as send_admins:
            result = await notify_account_edit(self.session, user=customer, event="block", actor="shop", shop_id=SHOP)
        send_admins.assert_not_awaited()
        self.assertEqual(result, {"subject": True, "admins": False})
        self.assertEqual(self.shop_bot.sent[0][0], CUSTOMER_TG)
        self.assertEqual(self.platform_bot.sent, [], "the shop's customer never hears from the platform bot")

    async def test_shop_staff_bulk_blocking_passes_their_shop(self) -> None:
        staff = {"role": "reseller", "bot_user_id": SHOP, "user_id": SHOP}
        with patch("app.services.notifications.notify_account_edit", AsyncMock()) as notify:
            ok, fail = await bulk_toggle_block(self.session, staff, [10, 11], block=True)
        self.assertEqual((ok, fail), (1, 1), "the platform customer is out of the shop's scope")
        self.assertEqual(notify.await_args.kwargs["shop_id"], SHOP)

    async def test_platform_account_edits_still_copy_platform_admins(self) -> None:
        customer = await self.session.get(BotUser, 11)
        with patch.object(notifications, "_send_admins", AsyncMock()) as send_admins:
            result = await notify_account_edit(self.session, user=customer, event="block", actor="owner")
        send_admins.assert_awaited_once()
        self.assertTrue(result["admins"])


if __name__ == "__main__":
    unittest.main()
