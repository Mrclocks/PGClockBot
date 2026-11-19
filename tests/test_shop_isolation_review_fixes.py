"""Rewards follow one shop from list to redemption; returning customers cost one lookup; broadcasts follow membership."""

from __future__ import annotations

import unittest


from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, BotUserShop, LoyaltyReward, Order, OrderStatus, ResellerProfile, ShopPoints
from app.services.broadcast import list_broadcast_targets
from app.services.loyalty import list_active_rewards, redeem_reward
from app.services.users import get_or_create_user, reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine

SHOP_ID, CUSTOMER, HISTORY, SHOP_ONLY = 5, 7, 8, 9
PLATFORM_REWARD, SHOP_REWARD = 1, 2


class ReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=CUSTOMER, telegram_id=107, referral_code="c", role="user", reseller_id=SHOP_ID, points_balance=100),
                BotUser(id=HISTORY, telegram_id=108, referral_code="h", role="user"),
                BotUser(id=SHOP_ONLY, telegram_id=109, referral_code="s", role="user", reseller_id=SHOP_ID),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True))
            session.add(Order(user_id=HISTORY, reseller_id=None, amount=1, status=OrderStatus.DELIVERED.value))
            session.add_all([
                ShopPoints(user_id=CUSTOMER, reseller_id=SHOP_ID, balance=100),
                BotUserShop(bot_user_id=CUSTOMER, shop_id=None),
                BotUserShop(bot_user_id=CUSTOMER, shop_id=SHOP_ID),
                BotUserShop(bot_user_id=SHOP_ONLY, shop_id=SHOP_ID),
                LoyaltyReward(id=PLATFORM_REWARD, name="platform", reward_type="wallet_credit", reward_value=100,
                              points_cost=10, enabled=True, reseller_id=None),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_a_shop_without_rewards_shows_none_and_a_platform_reward_cannot_be_redeemed_there(self):
        async with self.Session() as session:
            self.assertEqual(await list_active_rewards(session, reseller_id=SHOP_ID), [])
            self.assertEqual([r.id for r in await list_active_rewards(session, reseller_id=None)], [PLATFORM_REWARD])
        token = set_shop_reseller_id(SHOP_ID)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER)
                with self.assertRaisesRegex(ValueError, "فعال نیست"):
                    await redeem_reward(session, customer, PLATFORM_REWARD, idempotency_key="k1")
        finally:
            reset_shop_reseller_id(token)

    async def test_a_shop_reward_is_listed_and_redeemable_only_in_that_shop(self):
        async with self.Session() as session:
            session.add(LoyaltyReward(id=SHOP_REWARD, name="shop", reward_type="wallet_credit", reward_value=50,
                                      points_cost=10, enabled=True, reseller_id=SHOP_ID))
            await session.commit()
            self.assertEqual([r.id for r in await list_active_rewards(session, reseller_id=SHOP_ID)], [SHOP_REWARD])
            customer = await session.get(BotUser, CUSTOMER)
            with self.assertRaisesRegex(ValueError, "فعال نیست"):  # platform bot
                await redeem_reward(session, customer, SHOP_REWARD, idempotency_key="k2")
        token = set_shop_reseller_id(SHOP_ID)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER)
                redemption = await redeem_reward(session, customer, SHOP_REWARD, idempotency_key="k3")
                self.assertIsNotNone(redemption)
        finally:
            reset_shop_reseller_id(token)

    async def test_a_returning_cross_bot_customer_costs_one_extra_lookup_and_no_write(self):
        async with self.Session() as session:
            await get_or_create_user(session, 108, reseller_owner_id=SHOP_ID)  # first visit records the membership
        statements: list[str] = []
        commits: list[int] = []
        sync_engine = self.engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
        event.listen(sync_engine, "commit", lambda *_: commits.append(1))
        async with self.Session() as session:
            user = await get_or_create_user(session, 108, reseller_owner_id=SHOP_ID)
        self.assertIsNone(user.reseller_id)
        extra = [s for s in statements if "bot_user_shops" in s or "orders" in s or "user_services" in s]
        self.assertEqual(len(extra), 1, extra)
        self.assertEqual(commits, [])

    async def test_the_platform_broadcast_reaches_platform_members_only(self):
        async with self.Session() as session:
            ids = {int(u.id) for u in await list_broadcast_targets(session, audience="all")}
        self.assertIn(CUSTOMER, ids)        # attributed to a shop, also a platform customer
        self.assertIn(HISTORY, ids)         # unattributed
        self.assertNotIn(SHOP_ONLY, ids)    # only a customer of the shop
