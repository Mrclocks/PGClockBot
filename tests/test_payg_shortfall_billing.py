"""When a PAYG wallet cannot pay the whole usage delta, only the paid bytes are consumed."""

from __future__ import annotations

import unittest

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.db.models  # noqa: F401
from app.db import Base
from app.db.models import BotUser, ResellerBillingTransaction, ResellerPlan, ResellerProfile, Role
from app.services.billing import GB, tick_reseller_usage
from app.services.resellers import make_reseller
from tests.postgres_support import scoped_async_engine

RATE = 1000


class ShortfallBillingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _reseller(self, session: AsyncSession, wallet: int) -> tuple[ResellerProfile, BotUser]:
        plan = ResellerPlan(name="p", billing_mode="payg", price_per_gb=RATE, is_active=True)
        session.add(plan)
        user = BotUser(
            telegram_id=8801, role=Role.USER.value, referral_code="sf1", wallet_balance=wallet
        )
        session.add(user)
        await session.flush()
        profile = await make_reseller(
            session,
            user,
            web_username="shortfall",
            web_password_hash="x" * 24,
            plan_id=plan.id,
            billing_mode="payg",
        )
        return profile, user

    async def _usage_rows(self, session: AsyncSession) -> int:
        return int(
            await session.scalar(select(func.count()).select_from(ResellerBillingTransaction)) or 0
        )

    async def test_partial_wallet_consumes_only_paid_bytes(self) -> None:
        async with self.Session() as session:
            profile, user = await self._reseller(session, wallet=600)
            tx = await tick_reseller_usage(session, profile, {"lifetime_used_traffic": GB})
            self.assertEqual(tx.amount, -600)
            self.assertEqual(user.wallet_balance, 0)
            self.assertEqual(profile.billing_watermark_bytes, -(-600 * GB // RATE))
            self.assertLess(profile.billing_watermark_bytes, GB)

    async def test_remainder_is_billed_after_a_topup(self) -> None:
        async with self.Session() as session:
            profile, user = await self._reseller(session, wallet=600)
            await tick_reseller_usage(session, profile, {"lifetime_used_traffic": GB})
            user.wallet_balance = 5000
            await session.commit()
            tx = await tick_reseller_usage(session, profile, {"lifetime_used_traffic": GB})
            self.assertIn(tx.amount, (-399, -400))
            self.assertEqual(profile.billing_watermark_bytes, GB)
            self.assertEqual(user.wallet_balance, 5000 + tx.amount)
            self.assertLessEqual(600 - tx.amount, RATE)

    async def test_empty_wallet_holds_usage_without_a_ledger_row(self) -> None:
        async with self.Session() as session:
            profile, user = await self._reseller(session, wallet=0)
            tx = await tick_reseller_usage(session, profile, {"lifetime_used_traffic": GB})
            self.assertIsNone(tx)
            self.assertEqual(profile.billing_watermark_bytes, 0)
            self.assertEqual(await self._usage_rows(session), 0)

    async def test_funded_wallet_still_advances_fully(self) -> None:
        async with self.Session() as session:
            profile, user = await self._reseller(session, wallet=10_000)
            tx = await tick_reseller_usage(session, profile, {"lifetime_used_traffic": GB})
            self.assertEqual(tx.amount, -RATE)
            self.assertEqual(profile.billing_watermark_bytes, GB)
            self.assertEqual(user.wallet_balance, 10_000 - RATE)


if __name__ == "__main__":
    unittest.main()
