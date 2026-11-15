"""A shop's Mini App purchases tell the shop's staff, once, through the shop's own bot."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa

from app.db.models import BotUser, Order, OrderStatus
from app.services import purchase_notices
from app.services.wallet import credit_wallet
from tests.test_miniapp_shop_extras import SHOP_PLAN, ShopExtrasFixture
from tests.test_miniapp_shop_readonly import SHOP

OWNER_TELEGRAM_ID = 5005
OTHER_SHOP_OWNER_TELEGRAM_ID = 6006


class MiniPurchaseNoticeTests(ShopExtrasFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()

        async def deliver(session, order, *_args, **_kwargs):
            order.status = OrderStatus.DELIVERED.value
            await session.commit()
            return order

        delivery = patch("app.services.orders.fulfill_paid_order", AsyncMock(side_effect=deliver))
        delivery.start()
        self.patches.append(delivery)
        async with self.Session() as session:
            await credit_wallet(session, await session.get(BotUser, 10), 20_000, reason="test", shop_id=SHOP)

    def notices(self) -> list[tuple[int, str]]:
        return [(call.args[0], call.args[1]) for call in self.shop_notify_bot.send_message.await_args_list]

    async def buy(self):
        return await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 5_000})

    async def test_a_wallet_purchase_tells_the_shop_owner_through_the_shop_bot(self) -> None:
        response = await self.buy()
        self.assertEqual(response.status_code, 200, response.text)
        [(chat_id, text)] = self.notices()
        self.assertEqual(chat_id, OWNER_TELEGRAM_ID)
        self.assertIn("اشتراک جدید", text)
        self.assertIn(f"#{response.json()['order_id']}", text)
        self.assertIn("shop plan", text)
        self.assertNotIn(OTHER_SHOP_OWNER_TELEGRAM_ID, [chat for chat, _ in self.notices()])

    async def test_a_discounted_checkout_tells_the_shop_owner_when_paid(self) -> None:
        start = await self.post("/api/mini/checkout", {"plan_id": SHOP_PLAN, "price": 5_000, "code": "SHOPOFF"})
        order_id = start.json()["checkout"]["order_id"]
        self.assertEqual(self.notices(), [], "starting a checkout announces nothing")
        pay = await self.post(f"/api/mini/checkout/{order_id}/pay")
        self.assertEqual(pay.status_code, 200, pay.text)
        [(chat_id, text)] = self.notices()
        self.assertEqual(chat_id, OWNER_TELEGRAM_ID)
        self.assertIn(f"#{order_id}", text)

    async def test_a_refused_purchase_tells_no_one(self) -> None:
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 1})
        self.assertEqual(response.status_code, 400, "the price on the screen no longer matches")
        self.assertEqual(self.notices(), [])

    async def test_the_same_order_is_announced_once(self) -> None:
        response = await self.buy()
        async with self.Session() as session:
            order = await session.get(Order, response.json()["order_id"])
            user = await session.get(BotUser, 10)
            await purchase_notices.notify_purchase_outside_bot(session, order=order, user=user)
            self.assertIsNotNone((await session.get(Order, order.id)).staff_notified_at)
        self.assertEqual(len(self.notices()), 1)

    async def test_an_undelivered_order_is_not_announced(self) -> None:
        async with self.Session() as session:
            order = Order(user_id=10, plan_id=SHOP_PLAN, reseller_id=SHOP, amount=5_000,
                          status=OrderStatus.PAID.value)
            session.add(order)
            await session.commit()
            await purchase_notices.notify_purchase_outside_bot(
                session, order=order, user=await session.get(BotUser, 10)
            )
        self.assertEqual(self.notices(), [])

    async def test_a_failed_notice_keeps_the_purchase(self) -> None:
        failures = (
            AsyncMock(side_effect=RuntimeError("bot down")),
            AsyncMock(return_value=(None, False)),
        )
        for opener in failures:
            with self.subTest(opener=opener), patch("app.services.reseller_bots.open_notify_bot_for_reseller", opener):
                response = await self.buy()
                self.assertEqual(response.status_code, 200, response.text)
                async with self.Session() as session:
                    order = await session.get(Order, response.json()["order_id"])
                    self.assertEqual(order.status, OrderStatus.DELIVERED.value)
        self.assertEqual(self.notices(), [])


class StaffNotifiedMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_existing_orders(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0109_order_staff_notified_at.py"
        spec = importlib.util.spec_from_file_location("staff_notified_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER)"))
            conn.execute(sa.text("INSERT INTO orders (id, amount) VALUES (1, 500)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIsNone(conn.execute(sa.text("SELECT staff_notified_at FROM orders")).scalar())
                migration.downgrade()
                columns = {column["name"] for column in sa.inspect(conn).get_columns("orders")}
                self.assertNotIn("staff_notified_at", columns)
                self.assertEqual(conn.execute(sa.text("SELECT amount FROM orders")).scalar(), 500)
                migration.downgrade()
        engine.dispose()
