"""A customer's loyalty points are kept per shop and never cross between shops or the platform."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, LoyaltyReward, PointsRule, PointsTransaction, ResellerProfile, ShopPoints
from app.services.loyalty import (
    admin_adjust_points,
    apply_event_rules,
    credit_points,
    debit_points,
    list_points_history,
    points_balance,
    redeem_reward,
    reverse_points_tx,
)
from app.services.users import delete_bot_user, reset_shop_reseller_id, set_shop_reseller_id

SHOP_ID, CUSTOMER = 5, 7


class ShopPointsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'p.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=CUSTOMER, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True))
            await session.commit()
        self.keys = 0

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmp.cleanup()

    async def credit(self, session, amount: int, shop: int | None) -> PointsTransaction:
        self.keys += 1
        customer = await session.get(BotUser, CUSTOMER)
        return await credit_points(
            session, customer, amount, tx_type="earn", source="test", reference=None, description="t",
            idempotency_key=f"k{self.keys}", shop_id=shop,
        )

    async def balances(self) -> tuple[int, int]:
        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER)
            return await points_balance(session, customer, None), await points_balance(session, customer, SHOP_ID)

    async def test_credits_land_in_their_own_purse_and_are_stamped(self):
        async with self.Session() as session:
            platform_tx = await self.credit(session, 30, None)
            shop_tx = await self.credit(session, 70, SHOP_ID)
        self.assertEqual(await self.balances(), (30, 70))
        self.assertEqual((platform_tx.reseller_id, shop_tx.reseller_id), (None, SHOP_ID))
        self.assertEqual((platform_tx.balance_after, shop_tx.balance_after), (30, 70))

    async def test_a_shop_debit_cannot_use_the_platform_points(self):
        async with self.Session() as session:
            await self.credit(session, 100, None)
            customer = await session.get(BotUser, CUSTOMER)
            with self.assertRaisesRegex(ValueError, "امتیاز کافی نیست"):
                await debit_points(session, customer, 10, tx_type="spend", source="t", reference=None,
                                   description="t", idempotency_key="d1", shop_id=SHOP_ID)
        self.assertEqual(await self.balances(), (100, 0))

    async def test_concurrent_debits_cannot_overdraw_a_shop_purse(self):
        async with self.Session() as session:
            await self.credit(session, 10, SHOP_ID)

        async def spend(key: str) -> bool:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER)
                try:
                    await debit_points(session, customer, 10, tx_type="spend", source="t", reference=None,
                                       description="t", idempotency_key=key, shop_id=SHOP_ID)
                    return True
                except ValueError:
                    return False

        results = await asyncio.gather(spend("c1"), spend("c2"))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(await self.balances(), (0, 0))

    async def test_a_repeated_key_does_not_credit_twice(self):
        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER)
            for _ in range(2):
                await credit_points(session, customer, 5, tx_type="earn", source="t", reference=None,
                                    description="t", idempotency_key="same", shop_id=SHOP_ID)
        self.assertEqual(await self.balances(), (0, 5))

    async def test_a_reversal_returns_to_the_purse_of_the_original(self):
        async with self.Session() as session:
            tx = await self.credit(session, 40, SHOP_ID)
            await reverse_points_tx(session, tx, reason="refund")
        self.assertEqual(await self.balances(), (0, 0))

    async def test_history_shows_only_the_purse_asked_for(self):
        async with self.Session() as session:
            await self.credit(session, 1, None)
            await self.credit(session, 2, SHOP_ID)
            platform = await list_points_history(session, CUSTOMER, shop_id=None)
            shop = await list_points_history(session, CUSTOMER, shop_id=SHOP_ID)
        self.assertEqual([t.amount for t in platform], [1])
        self.assertEqual([t.amount for t in shop], [2])

    async def test_admin_adjustment_targets_one_purse(self):
        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER)
            await admin_adjust_points(session, customer, 25, reason="gift", admin_identity="a", shop_id=SHOP_ID, request_key="k1")
        self.assertEqual(await self.balances(), (0, 25))

    async def test_an_earn_rule_credits_the_shop_it_belongs_to(self):
        async with self.Session() as session:
            session.add(PointsRule(name="shop rule", event_key="purchase", amount_mode="fixed", amount=9,
                                   enabled=True, reseller_id=SHOP_ID))
            await session.commit()
            from app.services.loyalty import ensure_loyalty_defaults
            from app.services.users import set_setting

            await ensure_loyalty_defaults(session, reseller_id=SHOP_ID)
            await set_setting(session, "loyalty_enabled", "1", reseller_id=SHOP_ID)
            customer = await session.get(BotUser, CUSTOMER)
            await apply_event_rules(session, user=customer, event_key="purchase", idempotency_prefix="o1",
                                    reseller_id=SHOP_ID)
        platform, shop = await self.balances()
        self.assertEqual(platform, 0)
        self.assertGreaterEqual(shop, 9)

    async def redeem(self, bot_shop: int | None):
        token = set_shop_reseller_id(bot_shop)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER)
                return await redeem_reward(session, customer, 2, idempotency_key=f"r{bot_shop}")
        finally:
            reset_shop_reseller_id(token)

    async def test_redeeming_in_a_shop_bot_spends_that_shops_points_only(self):
        async with self.Session() as session:
            session.add(LoyaltyReward(id=2, name="credit", reward_type="wallet_credit", reward_value=100,
                                      points_cost=20, enabled=True, reseller_id=SHOP_ID))
            await session.commit()
            await self.credit(session, 500, None)
        with self.assertRaisesRegex(ValueError, "امتیاز کافی نیست"):
            await self.redeem(SHOP_ID)
        async with self.Session() as session:
            await self.credit(session, 20, SHOP_ID)
        await self.redeem(SHOP_ID)
        self.assertEqual(await self.balances(), (500, 0))

    async def test_deleting_a_shop_removes_its_points_and_keeps_the_platforms(self):
        async with self.Session() as session:
            await self.credit(session, 11, None)
            await self.credit(session, 22, SHOP_ID)
            await delete_bot_user(session, SHOP_ID, delete_pg_services=False, delete_pg_admin=False)
        async with self.Session() as session:
            rows = await session.scalar(select(func.count()).select_from(ShopPoints))
            ledger = {t.amount for t in await session.scalars(select(PointsTransaction))}
            platform = await points_balance(session, await session.get(BotUser, CUSTOMER), None)
        self.assertEqual((rows, ledger, platform), (0, {11}, 11))
