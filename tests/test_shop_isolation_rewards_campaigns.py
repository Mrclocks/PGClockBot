"""Rewards, the wheel and campaigns follow the bot in use and the service's shop, not attribution."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, BotUserShop, LoyaltyReward, ResellerProfile, ShopPoints, UserService
from app.services.campaigns import audience_query
from app.services.loyalty import points_balance, redeem_reward
from app.services.lucky_wheel import shop_prize_scope
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine

SHOP_ID = 5
CUSTOMER = 7
PLATFORM_SVC, SHOP_SVC = 31, 32


def _active_quota(now: datetime, *, days: int = 3) -> dict:
    return dict(
        quota_status="active", quota_synced_at=now, quota_expire_at=now + timedelta(days=days),
    )


class RewardAndCampaignScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                # Attributed to the shop, but also a platform customer.
                BotUser(id=CUSTOMER, telegram_id=107, referral_code="cust", role="user",
                        reseller_id=SHOP_ID, points_balance=100),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True))
            session.add_all([
                BotUserShop(bot_user_id=CUSTOMER, shop_id=None),
                BotUserShop(bot_user_id=CUSTOMER, shop_id=SHOP_ID),
                ShopPoints(user_id=CUSTOMER, reseller_id=SHOP_ID, balance=100),
                UserService(id=PLATFORM_SVC, bot_user_id=CUSTOMER, pg_user_id=1, pg_username="plat",
                            remark="order:1", shop_id=None, **_active_quota(self.now)),
                UserService(id=SHOP_SVC, bot_user_id=CUSTOMER, pg_user_id=2, pg_username="shop",
                            remark="order:2", shop_id=SHOP_ID, **_active_quota(self.now, days=20)),
                LoyaltyReward(id=1, name="days", reward_type="time_days", reward_value=1, points_cost=1,
                              enabled=True, reseller_id=None),
                LoyaltyReward(id=2, name="shop days", reward_type="time_days", reward_value=1, points_cost=1,
                              enabled=True, reseller_id=SHOP_ID),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def audience(self, shop: int | None, audience: str = "expiring", days: int = 7) -> set[int]:
        async with self.Session() as session:
            return {int(i) for i in await session.scalars(
                audience_query(shop_id=shop, audience=audience, days=days, now=self.now)
            )}

    async def test_a_platform_service_makes_the_customer_a_platform_expiring_target_only(self):
        self.assertEqual(await self.audience(None), {CUSTOMER})
        self.assertEqual(await self.audience(SHOP_ID), set())

    async def test_a_shop_service_makes_the_customer_that_shops_target_only(self):
        self.assertEqual(await self.audience(SHOP_ID, days=30), {CUSTOMER})
        self.assertEqual(await self.audience(None, days=30), {CUSTOMER})  # the platform service still expires in 3 days
        async with self.Session() as session:
            platform_service = await session.get(UserService, PLATFORM_SVC)
            platform_service.is_cancelled = True
            await session.commit()
        self.assertEqual(await self.audience(None, days=30), set())

    async def test_a_member_without_attribution_is_in_the_shops_audience(self):
        async with self.Session() as session:
            session.add(BotUser(id=8, telegram_id=108, referral_code="m", role="user",
                                created_at=self.now - timedelta(days=10)))
            await session.flush()
            session.add(BotUserShop(bot_user_id=8, shop_id=SHOP_ID))
            await session.commit()
        self.assertIn(8, await self.audience(SHOP_ID, audience="inactive", days=1))

    async def redeem(self, bot_shop: int | None, service_id: int):
        token = set_shop_reseller_id(bot_shop)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER)
                return await redeem_reward(
                    session, customer, 1 if bot_shop is None else 2, service_id=service_id, idempotency_key="k"
                )
        finally:
            reset_shop_reseller_id(token)

    async def test_a_reward_cannot_be_applied_to_a_service_another_shop_sold(self):
        for bot_shop, service_id in ((None, SHOP_SVC), (SHOP_ID, PLATFORM_SVC)):
            with self.assertRaisesRegex(ValueError, "سرویس معتبر نیست"):
                await self.redeem(bot_shop, service_id)
        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER)
            self.assertEqual(customer.points_balance, 100)
            self.assertEqual(await points_balance(session, customer, SHOP_ID), 100)

    async def test_a_platform_bot_reward_cannot_target_a_shop_service(self):
        with self.assertRaises(ValueError):
            await self.redeem(None, SHOP_SVC)

    async def test_wheel_scope_is_the_bot_in_use_not_the_customers_attribution(self):
        self.assertIsNone(shop_prize_scope())
        token = set_shop_reseller_id(SHOP_ID)
        try:
            self.assertEqual(shop_prize_scope(), SHOP_ID)
        finally:
            reset_shop_reseller_id(token)
        self.assertIsNone(shop_prize_scope())
