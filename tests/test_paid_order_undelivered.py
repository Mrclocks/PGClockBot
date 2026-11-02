"""A gateway-paid order whose service could not be created: money settled once, staff alerted,
the buyer told the truth, and staff resume delivery without a second charge or a second user."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    DeliveryFailure,
    Order,
    Payment,
    PaymentSettlement,
    Plan,
    ResellerProfile,
    ResellerSetting,
    SettlementStatus,
    UserService,
)
from app.services.delivery import UNDELIVERED_ORDER_NOTE, build_delivery_content
from app.services.orders import CUSTOM_PLAN_UNCONFIGURED, approve_payment, create_custom_order
from app.services.payment_settlement import settle_and_approve
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

SHOP = 3
CUSTOM = {"custom_plan_enabled": "1", "custom_plan_price_per_gb": "1000", "custom_plan_price_per_day": "100"}


class PanelDown(Exception):
    pass


class UndeliveredPaidOrderTestCase(unittest.IsolatedAsyncioTestCase):
    shop_id: int | None = None

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=11, referral_code="buyer", role="user"),
            BotUser(id=SHOP, telegram_id=33, referral_code="shop", role="reseller"),
            ResellerProfile(user_id=SHOP, pg_admin_username="shopadmin", is_active=True),
            Plan(id=1, name="catalog", price=10, is_active=True, pg_template_id=1, owner_reseller_id=self.shop_id),
            ResellerSetting(reseller_user_id=SHOP, key="custom_plan_group_ids", value="4"),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(self.shop_id)
        self.settings = {**CUSTOM, "custom_plan_group_ids": "2"}
        self.pg = SimpleNamespace(
            _login_username="shopadmin",
            create_user=AsyncMock(side_effect=PanelDown("panel timeout")),
            create_user_from_template=AsyncMock(),
            delete_user_by_id=AsyncMock(),
        )
        self.stuck_notice = AsyncMock()
        self.patches = [
            patch("app.services.users.get_all_settings", AsyncMock(side_effect=lambda *a, **k: dict(self.settings))),
            patch("app.services.orders.require_purchase_contact", AsyncMock()),
            patch("app.services.orders.get_pg", return_value=self.pg),
            patch("app.services.pasarguard.get_pg_for_reseller", AsyncMock(return_value=self.pg)),
            # Shop quota and volume holds are not under test here.
            patch("app.services.orders.assert_provision_create", AsyncMock()),
            patch("app.services.orders._hold_delivery_volume", AsyncMock(return_value=None)),
            patch("app.services.orders.release_volume_hold", AsyncMock()),
            patch("app.services.loyalty.on_order_delivered", AsyncMock()),
            patch("app.services.notifications.notify_approved_delivery_stuck", self.stuck_notice),
            patch("app.bot.create_bot", return_value=SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def paid_custom_order(self) -> SimpleNamespace:
        order = await create_custom_order(self.session, user_id=1, data_limit_gb=10, duration_days=30)
        payment = Payment(user_id=1, order_id=order.id, amount=order.amount, status="pending", method="online")
        self.session.add(payment)
        await self.session.flush()
        settlement = PaymentSettlement(
            payment_id=payment.id, shop_owner_id=self.shop_id, tenant_key=int(self.shop_id or 0),
            channel="psp", provider="mock", status=SettlementStatus.AWAITING.value, amount=order.amount,
            idempotency_key=f"psp:{payment.id}", external_ref="ref-1",
        )
        self.session.add(settlement)
        await self.session.commit()
        # Plain values: settle_and_approve rolls the session back, which expires the rows.
        return SimpleNamespace(order_id=int(order.id), payment_id=int(payment.id), settlement=settlement,
                               settlement_id=int(settlement.id), amount=int(order.amount))

    async def settle(self, paid: SimpleNamespace) -> Payment:
        settlement = await self.fresh(PaymentSettlement, paid.settlement_id)
        return await settle_and_approve(self.session, settlement, expected_amount=paid.amount, external_ref="ref-1")

    async def fresh(self, model, row_id):
        self.session.expire_all()
        return await self.session.get(model, row_id)

    async def delivery_text(self, paid: SimpleNamespace) -> str:
        self.session.expire_all()
        payment = await self.session.get(Payment, paid.payment_id)
        order = await self.session.get(Order, paid.order_id)
        return (await build_delivery_content(self.session, payment, order))["text"]



class UndeliveredCases:
    """Run for the platform and for a shop."""

    async def test_failed_creation_settles_once_and_alerts_staff(self) -> None:
        paid = await self.paid_custom_order()
        result = await self.settle(paid)
        self.assertEqual(result.status, "approved")
        self.assertEqual((await self.fresh(PaymentSettlement, paid.settlement_id)).status, SettlementStatus.SETTLED.value,
                         "the money is confirmed; the gateway is not asked again")
        self.assertEqual((await self.fresh(Order, paid.order_id)).status, "paid")
        failures = await self.session.scalar(select(func.count()).select_from(DeliveryFailure).where(DeliveryFailure.order_id == paid.order_id))
        self.assertEqual(failures, 1, "the order is listed under failed deliveries")
        self.stuck_notice.assert_awaited_once()
        self.assertIn("panel timeout", self.stuck_notice.await_args.kwargs["error"])
        # A later poll or «بررسی پرداخت» finds it settled: no second create, no second alert.
        await self.settle(paid)
        self.assertEqual(self.pg.create_user.await_count, 1)
        self.stuck_notice.assert_awaited_once()

    async def test_buyer_is_told_the_service_is_not_ready_yet(self) -> None:
        paid = await self.paid_custom_order()
        await self.settle(paid)
        text = await self.delivery_text(paid)
        self.assertIn(UNDELIVERED_ORDER_NOTE, text)
        self.assertIn("در حال آماده‌سازی", text)

    async def test_staff_resume_delivers_once_without_a_second_charge(self) -> None:
        paid = await self.paid_custom_order()
        await self.settle(paid)
        self.pg.create_user = AsyncMock(return_value={"id": 9, "username": "u9", "subscription_url": "https://x/sub/t"})
        delivered = await approve_payment(self.session, await self.fresh(Payment, paid.payment_id), reviewer_tg=0)
        self.assertEqual(delivered.status, "delivered")
        services = list(await self.session.scalars(select(UserService).where(UserService.remark == f"order:{paid.order_id}")))
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].shop_id, self.shop_id)
        self.assertEqual(self.pg.create_user.await_args.args[0]["group_ids"], [4] if self.shop_id else [2])
        self.assertNotIn(UNDELIVERED_ORDER_NOTE, await self.delivery_text(paid))

    async def test_payment_failure_still_reopens_the_settlement(self) -> None:
        paid = await self.paid_custom_order()
        with patch("app.services.payment_settlement.approve_payment", AsyncMock(side_effect=ValueError("db busy"))):
            with self.assertRaises(ValueError):
                await self.settle(paid)
        self.assertEqual((await self.fresh(PaymentSettlement, paid.settlement_id)).status, SettlementStatus.AWAITING.value)
        self.stuck_notice.assert_not_awaited()


class PlatformUndeliveredPaidOrderTests(UndeliveredCases, UndeliveredPaidOrderTestCase):
    shop_id = None


class ShopUndeliveredPaidOrderTests(UndeliveredCases, UndeliveredPaidOrderTestCase):
    shop_id = SHOP


class CustomPlanConfigTests(UndeliveredPaidOrderTestCase):
    async def test_platform_custom_plan_without_template_or_groups_is_refused_before_payment(self) -> None:
        self.settings = dict(CUSTOM)
        with self.assertRaises(ValueError) as caught:
            await create_custom_order(self.session, user_id=1, data_limit_gb=10, duration_days=30)
        self.assertEqual(str(caught.exception), CUSTOM_PLAN_UNCONFIGURED)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(Order)), 0)

    async def test_platform_custom_plan_with_a_template_is_accepted(self) -> None:
        self.settings = {**CUSTOM, "custom_plan_template_id": "7"}
        order = await create_custom_order(self.session, user_id=1, data_limit_gb=10, duration_days=30)
        self.assertEqual(order.status, "pending")


class TemplateCustomPlanTests(UndeliveredPaidOrderTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.settings = {**CUSTOM, "custom_plan_template_id": "7"}
        self.pg.create_user_from_template = AsyncMock(return_value={"id": 9, "username": "u9", "subscription_url": "https://x/sub/t"})
        self.pg.modify_user_by_id = AsyncMock(return_value={"id": 9, "data_limit": 10 * 1024**3})

    async def approve(self) -> Order:
        order = await create_custom_order(self.session, user_id=1, data_limit_gb=10, duration_days=30)
        payment = Payment(user_id=1, order_id=order.id, amount=order.amount, status="pending", receipt_file_id="f")
        self.session.add(payment)
        await self.session.commit()
        return await approve_payment(self.session, payment, reviewer_tg=0)

    async def test_template_service_gets_the_volume_and_days_the_buyer_chose(self) -> None:
        delivered = await self.approve()
        self.assertEqual(delivered.status, "delivered")
        user_id, payload = self.pg.modify_user_by_id.await_args.args
        self.assertEqual(user_id, 9)
        self.assertEqual(payload["data_limit"], 10 * 1024**3)
        self.assertIn("expire", payload)

    async def test_catalog_template_plans_keep_the_template_limits(self) -> None:
        self.session.add(Order(id=50, user_id=1, plan_id=1, amount=10, status="paid"))
        await self.session.commit()
        from app.services.orders import deliver_order

        await deliver_order(self.session, await self.session.get(Order, 50))
        self.pg.modify_user_by_id.assert_not_awaited()

    async def test_failed_limits_remove_the_new_user_and_keep_the_order_paid(self) -> None:
        self.pg.modify_user_by_id = AsyncMock(side_effect=PanelDown("modify failed"))
        with self.assertRaises(PanelDown):
            await self.approve()
        self.pg.delete_user_by_id.assert_awaited_once_with(9)
        self.session.expire_all()
        order = await self.session.scalar(select(Order).where(Order.note == "custom"))
        self.assertEqual((order.status, order.service_id), ("paid", None))


if __name__ == "__main__":
    unittest.main()
