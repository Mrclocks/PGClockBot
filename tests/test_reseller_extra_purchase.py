"""Buying extra GB or users: refund only when the panel refused, never on an unknown outcome."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile, WalletTransaction
from app.services.billing import GB
from app.services.pasarguard import PasarGuardError
from app.services.reseller_capacity import (
    EXTRA_FAILED_REFUNDED,
    EXTRA_OUTCOME_UNKNOWN,
    buy_extra_gb,
    buy_extra_users,
)


class ExtraPurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=100_000),
                ResellerPlan(
                    id=1, name="p", plan_kind="subscription", billing_mode="fixed", included_users=10, included_gb=100,
                    price=1, allow_buy_extra=True, extra_gb_price=1_000, extra_user_price=2_000, is_active=True,
                ),
                ResellerProfile(id=1, user_id=3, pg_admin_username="shop3", plan_id=1, is_active=True),
                PgAdminSubscription(pg_username="shop3", plan_id=1, access_status="active", base_users=10, base_gb=100),
            ])
            await session.commit()
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(return_value={"data_limit": 100 * GB, "max_users": 10}),
            modify_admin=AsyncMock(return_value={}),
        )
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.reseller_capacity.assert_child_growth_fits", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def buy(self, kind: str, amount: int) -> dict:
        async with self.Session() as session:
            user = await session.get(BotUser, 3)
            profile = await session.get(ResellerProfile, 1)
            plan = await session.get(ResellerPlan, 1)
            if kind == "gb":
                return await buy_extra_gb(session, user=user, profile=profile, plan=plan, gb=amount)
            return await buy_extra_users(session, user=user, profile=profile, plan=plan, count=amount)

    async def state(self) -> tuple[int, int, int, int]:
        async with self.Session() as session:
            wallet = int((await session.get(BotUser, 3)).wallet_balance)
            sub = await session.scalar(select(PgAdminSubscription))
            rows = len(list(await session.scalars(select(WalletTransaction))))
            return wallet, int(sub.extra_gb_purchased or 0), int(sub.extra_users_purchased or 0), rows

    async def test_success_charges_and_records_once(self) -> None:
        result = await self.buy("gb", 5)
        self.assertEqual(result["data_limit"], 105 * GB)
        self.assertEqual(await self.state(), (95_000, 5, 0, 1))

    async def test_panel_refusal_refunds_and_forgets_the_extra(self) -> None:
        self.pg.modify_admin = AsyncMock(side_effect=PasarGuardError("bad", 422))
        with self.assertRaises(ValueError) as caught:
            await self.buy("users", 5)
        self.assertEqual(str(caught.exception), EXTRA_FAILED_REFUNDED)
        self.assertEqual(await self.state(), (100_000, 0, 0, 2))

    async def test_unreadable_admin_refunds_before_any_write(self) -> None:
        self.pg.get_admin = AsyncMock(side_effect=httpx.ConnectTimeout("down"))
        with self.assertRaises(ValueError):
            await self.buy("gb", 1)
        self.assertEqual((await self.state())[:3], (100_000, 0, 0))
        self.pg.modify_admin.assert_not_awaited()

    async def test_unknown_outcome_keeps_the_purchase(self) -> None:
        for error in (httpx.ReadTimeout("slow"), PasarGuardError("gateway", 502), PasarGuardError("transport")):
            with self.subTest(error=repr(error)):
                self.pg.modify_admin = AsyncMock(side_effect=error)
                before = await self.state()
                with self.assertRaises(ValueError) as caught:
                    await self.buy("gb", 1)
                self.assertEqual(str(caught.exception), EXTRA_OUTCOME_UNKNOWN)
                after = await self.state()
                self.assertEqual((after[0], after[1]), (before[0] - 1_000, before[1] + 1), "no blind refund")

    async def test_short_wallet_records_nothing(self) -> None:
        await self.buy("gb", 50)
        await self.buy("gb", 50)
        with self.assertRaises(ValueError):
            await self.buy("gb", 1)
        self.assertEqual(await self.state(), (0, 100, 0, 2))


if __name__ == "__main__":
    unittest.main()
