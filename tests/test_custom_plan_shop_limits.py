"""A shop's custom plan size must be deliverable by its panel admin before the buyer pays."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, Plan, ResellerSetting
from app.services.orders import create_custom_order
from app.services.provision_gate import ProvisionError
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id

SETTINGS = {"custom_plan_enabled": "1", "custom_plan_price_per_gb": "100", "custom_plan_price_per_day": "100"}


class CustomPlanShopLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=2, telegram_id=102, referral_code="shop", role="reseller"),
            BotUser(id=1, telegram_id=101, referral_code="buyer", reseller_id=2),
            Plan(id=1, name="P", price=10, duration_days=30, data_limit_gb=1, owner_reseller_id=2, is_active=True),
            ResellerSetting(reseller_user_id=2, key="custom_plan_group_ids", value="3"),
        ])
        await self.session.commit()
        _SETTINGS_CACHE.clear()
        self.token = set_shop_reseller_id(2)

    async def asyncTearDown(self) -> None:
        reset_shop_reseller_id(self.token)
        await self.session.close()
        await self.engine.dispose()

    async def _create(self, gate: AsyncMock) -> Order:
        with patch("app.services.users.get_all_settings", AsyncMock(return_value=SETTINGS)), patch(
            "app.services.orders.assert_provision_create", gate
        ), patch("app.services.orders.require_purchase_contact", AsyncMock()):
            return await create_custom_order(self.session, user_id=1, data_limit_gb=100, duration_days=90)

    async def test_size_over_panel_role_limit_is_refused_without_creating_order(self) -> None:
        gate = AsyncMock(side_effect=ProvisionError("حجم کاربر نمی‌تواند بیشتر از 50 GB باشد"))
        with self.assertRaisesRegex(ValueError, "50 GB"):
            await self._create(gate)
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)
        self.assertEqual(await self.session.scalar(select(func.count(Plan.id))), 1)

    async def test_deliverable_size_creates_order_and_checks_requested_values(self) -> None:
        gate = AsyncMock()
        order = await self._create(gate)
        self.assertEqual(order.note, "custom")
        kwargs = gate.await_args.kwargs
        self.assertEqual(kwargs["reseller_user_id"], 2)
        self.assertEqual(kwargs["data_limit"], 100 * 1024**3)
        self.assertFalse(kwargs["from_template"])


if __name__ == "__main__":
    unittest.main()
