"""A person can be a customer of several shops; lists and scope follow membership, account-wide actions do not."""

from __future__ import annotations

import unittest


from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, BotUserShop, Order, OrderStatus, ResellerProfile
from app.services.shop_membership import (
    assert_customer_in_staff_scope,
    ensure_member,
    is_member,
    is_shop_customer,
    shop_member_clause,
)
from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope
from app.services.users import get_or_create_user
from app.services.users_ops import scoped_users_where
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION
from tests.postgres_support import scoped_async_engine

SHOP_ID = 5
HISTORY_USER, NEW_TG = 1, 999


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=HISTORY_USER, telegram_id=101, referral_code="hist", role="user"),
                BotUser(id=2, telegram_id=102, referral_code="plain", role="user"),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True))
            session.add(Order(user_id=HISTORY_USER, reseller_id=None, amount=1, status=OrderStatus.DELIVERED.value))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def member_ids(self, shop: int | None) -> set[int]:
        async with self.Session() as session:
            return {int(i) for i in await session.scalars(select(BotUser.id).where(shop_member_clause(shop)))}

    async def test_recording_a_membership_twice_keeps_one_row(self):
        async with self.Session() as session:
            for _ in range(3):
                await ensure_member(session, 2, SHOP_ID)
                await ensure_member(session, 2, None)
            await session.commit()
            rows = await session.scalar(select(func.count()).select_from(BotUserShop).where(BotUserShop.bot_user_id == 2))
        self.assertEqual(rows, 2)

    async def test_a_customer_with_history_opening_a_shop_bot_becomes_a_member_not_the_shops_user(self):
        async with self.Session() as session:
            user = await get_or_create_user(session, 101, reseller_owner_id=SHOP_ID)
            self.assertIsNone(user.reseller_id)
            self.assertTrue(await is_member(session, HISTORY_USER, SHOP_ID))
        self.assertIn(HISTORY_USER, await self.member_ids(SHOP_ID))
        self.assertIn(HISTORY_USER, await self.member_ids(None))

    async def test_a_new_visitor_of_a_shop_bot_belongs_to_that_shop_only(self):
        async with self.Session() as session:
            user = await get_or_create_user(session, NEW_TG, reseller_owner_id=SHOP_ID)
            new_id = int(user.id)
            self.assertEqual(user.reseller_id, SHOP_ID)
        self.assertIn(new_id, await self.member_ids(SHOP_ID))
        self.assertNotIn(new_id, await self.member_ids(None))

    async def test_a_new_visitor_of_the_platform_bot_is_a_platform_member(self):
        async with self.Session() as session:
            user = await get_or_create_user(session, NEW_TG)
            self.assertTrue(await is_member(session, int(user.id), None))

    async def test_staff_scope_accepts_a_member_but_account_wide_scope_does_not(self):
        async with self.Session() as session:
            await get_or_create_user(session, 101, reseller_owner_id=SHOP_ID)
            customer = await session.get(BotUser, HISTORY_USER)
            shop_staff = {**SHOP_SESSION, "permissions": ["dashboard"]}
            await assert_customer_in_staff_scope(session, shop_staff, customer)
            with self.assertRaises(ShopScopeError):
                assert_bot_user_in_scope(shop_staff, customer)
            await assert_customer_in_staff_scope(session, OWNER_SESSION, customer)

    async def test_a_stranger_is_outside_every_scope(self):
        async with self.Session() as session:
            stranger = await session.get(BotUser, 2)
            shop_staff = {**SHOP_SESSION, "permissions": ["dashboard"]}
            with self.assertRaises(ShopScopeError):
                await assert_customer_in_staff_scope(session, shop_staff, stranger)
            self.assertFalse(await is_shop_customer(session, stranger, SHOP_ID))

    async def test_user_lists_follow_membership_for_the_shop_and_the_platform(self):
        async with self.Session() as session:
            await get_or_create_user(session, 101, reseller_owner_id=SHOP_ID)
            shop_ids = {int(i) for i in await session.scalars(select(BotUser.id).where(scoped_users_where(SHOP_ID)))}
            platform_ids = {int(i) for i in await session.scalars(select(BotUser.id).where(scoped_users_where(None)))}
        self.assertEqual(shop_ids, {HISTORY_USER})
        self.assertIn(HISTORY_USER, platform_ids)
        self.assertIn(2, platform_ids)
