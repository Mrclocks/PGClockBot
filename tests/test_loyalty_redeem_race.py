"""Concurrent redemptions sharing one idempotency key spend and grant once, without crashing."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, LoyaltyReward, PointsTransaction, RewardRedemption, WalletTransaction
from app.services.loyalty import redeem_reward
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id


class RedeemRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'race.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="a", role="user", points_balance=500),
                LoyaltyReward(id=1, name="credit", reward_type="wallet_credit", reward_value=20_000, points_cost=100,
                              enabled=True, archived=False, redemption_count=0),
            ])
            await session.commit()
        self.shop_token = set_shop_reseller_id(None)

    async def asyncTearDown(self) -> None:
        reset_shop_reseller_id(self.shop_token)
        await self.engine.dispose()
        self.tmp.cleanup()

    async def redeem_once(self) -> int | None:
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            try:
                redemption = await redeem_reward(session, user, 1, idempotency_key="same-tap")
            except (ValueError, OperationalError):
                return None
            return int(redemption.id)

    async def count(self, model) -> int:
        async with self.Session() as session:
            return int(await session.scalar(select(func.count()).select_from(model)))

    async def test_same_key_concurrent_redeems_grant_once(self) -> None:
        for _ in range(3):
            ids = await asyncio.gather(*(self.redeem_once() for _ in range(4)))
            self.assertEqual(len({i for i in ids if i is not None}), 1, ids)
        self.assertEqual(await self.count(RewardRedemption), 1)
        self.assertEqual(await self.count(PointsTransaction), 1)
        self.assertEqual(await self.count(WalletTransaction), 1)
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            self.assertEqual((user.points_balance, user.wallet_balance), (400, 20_000))


if __name__ == "__main__":
    unittest.main()
