"""Services with an unknown shop are hidden until the owner assigns one, exactly once."""

from __future__ import annotations

import unittest


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, ResellerProfile, UserService
from app.services.service_scope import customer_may_use_service, list_customer_services
from app.services.service_shop_review import assign_service_shop, list_services_under_review
from app.services.shop_membership import is_member
from tests.postgres_support import scoped_async_engine

SHOP_ID, CUSTOMER_ID, REVIEW_SVC, SETTLED_SVC = 5, 7, 21, 22


class ServiceShopReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=CUSTOMER_ID, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True))
            session.add(Order(user_id=CUSTOMER_ID, reseller_id=None, amount=1, status=OrderStatus.DELIVERED.value))
            session.add_all([
                UserService(id=REVIEW_SVC, bot_user_id=CUSTOMER_ID, pg_user_id=1, pg_username="waiting",
                            remark="imported", shop_needs_review=True),
                UserService(id=SETTLED_SVC, bot_user_id=CUSTOMER_ID, pg_user_id=2, pg_username="settled",
                            remark="imported", shop_id=SHOP_ID),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_the_list_shows_only_services_under_review_with_their_evidence(self):
        async with self.Session() as session:
            rows = await list_services_under_review(session)
        self.assertEqual([(r.service_id, r.attributed_shop, r.order_shops) for r in rows], [(REVIEW_SVC, SHOP_ID, [None])])

    async def test_a_service_under_review_is_hidden_from_every_shop_and_from_the_customer(self):
        async with self.Session() as session:
            service = await session.get(UserService, REVIEW_SVC)
            for shop in (None, SHOP_ID):
                self.assertFalse(await customer_may_use_service(session, service, CUSTOMER_ID, shop))
                listed = {int(s.id) for s in await list_customer_services(session, CUSTOMER_ID, shop)}
                self.assertNotIn(REVIEW_SVC, listed)

    async def test_assigning_a_shop_releases_the_service_to_that_shop_only(self):
        async with self.Session() as session:
            service = await assign_service_shop(session, REVIEW_SVC, None)
            self.assertFalse(service.shop_needs_review)
            self.assertTrue(await customer_may_use_service(session, service, CUSTOMER_ID, None))
            self.assertFalse(await customer_may_use_service(session, service, CUSTOMER_ID, SHOP_ID))
            self.assertTrue(await is_member(session, CUSTOMER_ID, None))

    async def test_a_second_assignment_is_refused(self):
        async with self.Session() as session:
            await assign_service_shop(session, REVIEW_SVC, SHOP_ID)
            with self.assertRaises(ValueError):
                await assign_service_shop(session, REVIEW_SVC, None)
            with self.assertRaises(ValueError):
                await assign_service_shop(session, SETTLED_SVC, None)

    async def test_an_unknown_shop_is_refused_and_the_service_stays_under_review(self):
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await assign_service_shop(session, REVIEW_SVC, 999)
            self.assertTrue((await session.get(UserService, REVIEW_SVC)).shop_needs_review)
