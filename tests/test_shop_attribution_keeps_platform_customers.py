"""First contact with a shop bot must not move an existing customer into that shop."""

from __future__ import annotations

import unittest


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, ResellerProfile, UserService
from app.services.users import get_or_create_user
from tests.postgres_support import scoped_async_engine

SHOP_ID = 5


class ShopAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=1, telegram_id=101, referral_code="has-service", role="user"),
                BotUser(id=2, telegram_id=102, referral_code="has-order", role="user"),
                BotUser(id=3, telegram_id=103, referral_code="fresh", role="user"),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True))
            session.add(UserService(bot_user_id=1, pg_user_id=9, pg_username="platform_svc", remark="order:1"))
            session.add(Order(user_id=2, reseller_id=None, amount=1000, status=OrderStatus.PENDING.value))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def open_shop_bot(self, telegram_id: int) -> BotUser:
        async with self.Session() as session:
            return await get_or_create_user(session, telegram_id, reseller_owner_id=SHOP_ID)

    async def test_a_customer_with_a_service_stays_unattributed(self):
        self.assertIsNone((await self.open_shop_bot(101)).reseller_id)

    async def test_a_customer_with_an_order_stays_unattributed(self):
        self.assertIsNone((await self.open_shop_bot(102)).reseller_id)

    async def test_a_new_visitor_is_still_attributed_to_the_shop(self):
        self.assertEqual((await self.open_shop_bot(103)).reseller_id, SHOP_ID)

    async def test_a_first_time_user_is_attributed_on_creation(self):
        self.assertEqual((await self.open_shop_bot(999)).reseller_id, SHOP_ID)
