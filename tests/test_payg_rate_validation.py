"""PAYG plans must carry a positive per-GB rate, and usage is never forgiven at rate 0."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.db.models  # noqa: F401
from app.db import Base
from app.db.models import BotUser, ResellerPlan, Role
from app.services.billing import (
    GB,
    PaygRateError,
    require_positive_payg_rate,
    tick_reseller_usage,
)
from app.services.resellers import create_application, provision_existing_pg_admin
from tests.postgres_support import scoped_async_engine


class RequirePositiveRateTests(unittest.TestCase):
    def test_zero_negative_and_missing_rates_are_rejected(self) -> None:
        for bad in (0, -5, None):
            with self.assertRaises(PaygRateError):
                require_positive_payg_rate(bad)

    def test_positive_rate_is_returned(self) -> None:
        self.assertEqual(require_positive_payg_rate(1200), 1200)


class ZeroRateTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_rate_keeps_watermark_so_usage_is_billed_after_a_fix(self) -> None:
        profile = MagicMock()
        profile.billing_mode = "payg"
        profile.user_id = 7
        profile.plan_id = 3
        profile.billing_watermark_bytes = GB
        session = AsyncMock()
        with patch("app.services.billing.resolve_price_per_gb", AsyncMock(return_value=0)):
            result = await tick_reseller_usage(
                session, profile, {"lifetime_used_traffic": 3 * GB}, commit=True
            )
        self.assertIsNone(result)
        self.assertEqual(profile.billing_watermark_bytes, GB)


class ZeroRatePlanPurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _zero_rate_payg_plan(self, session: AsyncSession) -> ResellerPlan:
        plan = ResellerPlan(
            name="free-by-mistake",
            price=0,
            billing_mode="payg",
            price_per_gb=0,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        return plan

    async def test_customer_cannot_apply_for_zero_rate_payg_plan(self) -> None:
        async with self.Session() as session:
            plan = await self._zero_rate_payg_plan(session)
            user = BotUser(telegram_id=801, role=Role.USER.value, referral_code="zr1", wallet_balance=10**9)
            session.add(user)
            await session.flush()
            with self.assertRaises(PaygRateError):
                await create_application(session, user=user, plan=plan)

    async def test_admin_cannot_grant_zero_rate_payg_plan_to_existing_pg_admin(self) -> None:
        async with self.Session() as session:
            plan = await self._zero_rate_payg_plan(session)
            profile, hint, error = await provision_existing_pg_admin(
                session,
                pg_username="shop1",
                web_username="shop1",
                password="Str0ng-Passw0rd!",
                plan_id=plan.id,
            )
        self.assertIsNone(profile)
        self.assertIsNone(hint)
        self.assertIn("نرخ", error or "")


if __name__ == "__main__":
    unittest.main()
