"""Purchase-funnel steps recorded from the order flow: shop scope, idempotency and failure isolation."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, FunnelEvent, Order, OrderStatus, Payment, Plan
from app.services.funnel_events import STEP_PAY_START, record_order_funnel
from app.services.orders import attach_receipt, deliver_order
from app.services.users import clear_settings_cache, set_setting
from app.services.ux20 import funnel_summary
from tests.test_miniapp_shop_purchase import SHOP, SHOP_PLAN
from tests.test_miniapp_shop_purchase import ShopPurchaseTests as _ShopPurchaseTests


class FunnelCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=1001, referral_code="buyer"),
            Plan(id=1, name="Monthly", price=100, pg_template_id=1),
        ])
        await self.session.flush()
        self.session.add_all([
            Order(id=12, user_id=1, plan_id=1, amount=100, status=OrderStatus.PAID.value),
            Order(id=13, user_id=1, plan_id=1, amount=100, status=OrderStatus.AWAITING_RECEIPT.value, reseller_id=3),
            Order(id=14, user_id=1, plan_id=1, amount=100, status=OrderStatus.AWAITING_RECEIPT.value, note="renew:5"),
            Order(id=15, user_id=1, plan_id=1, amount=100, status=OrderStatus.AWAITING_RECEIPT.value),
        ])
        await self.session.flush()
        self.session.add_all([
            Payment(id=1, order_id=13, user_id=1, amount=100, status="pending"),
            Payment(id=2, order_id=14, user_id=1, amount=100, status="pending"),
            Payment(id=3, order_id=15, user_id=1, amount=100, status="pending"),
        ])
        await self.session.commit()
        clear_settings_cache()
        self.pg = SimpleNamespace(create_user_from_template=AsyncMock(return_value={
            "id": 901, "username": "svc", "subscription_url": "https://example.com/sub/svc",
        }))

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        clear_settings_cache()

    async def events(self):
        rows = await self.session.scalars(select(FunnelEvent).order_by(FunnelEvent.id))
        return [(e.step, e.order_id, e.reseller_id, e.user_id) for e in rows]

    async def deliver(self):
        order = await self.session.get(Order, 12)
        with patch("app.services.orders.get_pg", return_value=self.pg), patch("app.services.loyalty.on_order_delivered", AsyncMock()):
            return await deliver_order(self.session, order)


class FunnelEventTests(FunnelCase):
    async def test_delivery_records_one_delivered_event_for_the_order(self):
        delivered = await self.deliver()
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(await self.events(), [("delivered", 12, None, 1)])
        await self.deliver()
        self.assertEqual(len(await self.events()), 1)

    async def test_receipt_is_recorded_once_for_the_shop_of_the_order(self):
        payment = await self.session.get(Payment, 1)
        await attach_receipt(self.session, payment, "file-1")
        await attach_receipt(self.session, payment, "file-2")
        self.assertEqual(await self.events(), [("receipt", 13, 3, 1)])

    async def test_receipt_for_a_renewal_is_not_a_purchase_step(self):
        await attach_receipt(self.session, await self.session.get(Payment, 2), "file-1")
        self.assertEqual(await self.events(), [])

    async def test_the_shops_tracking_switch_is_honored_per_shop(self):
        await set_setting(self.session, "funnel_tracking_enabled", "0", reseller_id=3)
        clear_settings_cache()
        await attach_receipt(self.session, await self.session.get(Payment, 1), "file-1")
        self.assertEqual(await self.events(), [])
        await attach_receipt(self.session, await self.session.get(Payment, 3), "file-2")
        self.assertEqual(await self.events(), [("receipt", 15, None, 1)])

    async def test_a_recording_failure_never_fails_the_delivery(self):
        with patch("app.services.funnel_events.record_funnel_event", AsyncMock(side_effect=RuntimeError("boom"))):
            delivered = await self.deliver()
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(await self.events(), [])
        order = await self.session.get(Order, 12)
        await self.session.refresh(order)
        self.assertEqual(order.status, "delivered")

    async def test_summary_counts_each_shop_separately(self):
        await self.deliver()
        await attach_receipt(self.session, await self.session.get(Payment, 1), "file-1")
        platform = await funnel_summary(self.session, reseller_id=None)
        shop = await funnel_summary(self.session, reseller_id=3)
        self.assertEqual((platform["delivered"], platform["receipt"]), (1, 0))
        self.assertEqual((shop["delivered"], shop["receipt"]), (0, 1))

    async def test_the_same_key_never_records_twice(self):
        order = await self.session.get(Order, 12)
        for _ in range(3):
            await record_order_funnel(self.session, order, STEP_PAY_START, key="pay_start:order:12")
        self.assertEqual(await self.events(), [("pay_start", 12, None, 1)])


class MiniAppFunnelTests(_ShopPurchaseTests):
    async def test_mini_app_purchase_records_pay_start_for_the_shop(self):
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 5_000})
        self.assertEqual(response.status_code, 200, response.text)
        async with self.Session() as session:
            rows = list(await session.scalars(select(FunnelEvent)))
        self.assertEqual([(e.step, e.reseller_id) for e in rows], [("pay_start", SHOP)])

    async def test_a_refused_purchase_records_nothing(self):
        response = await self.post("/api/mini/buy", {"plan_id": 999})
        self.assertEqual(response.status_code, 400)
        async with self.Session() as session:
            self.assertEqual(list(await session.scalars(select(FunnelEvent))), [])


if __name__ == "__main__":
    unittest.main()
