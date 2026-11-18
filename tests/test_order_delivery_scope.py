"""Delivery recovery must select only the buyer's exact order reference."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, Order, OrderStatus, Plan, UserService
from app.services.orders import deliver_order
from tests.postgres_support import scoped_async_engine


class OrderDeliveryScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.buyer = BotUser(telegram_id=91001, referral_code="delivery-buyer")
        self.other = BotUser(telegram_id=91002, referral_code="delivery-other")
        self.plan = Plan(name="Monthly", price=100, pg_template_id=1)
        self.session.add_all([self.buyer, self.other, self.plan])
        await self.session.flush()
        self.order = Order(
            id=12,
            user_id=self.buyer.id,
            plan_id=self.plan.id,
            amount=100,
            status=OrderStatus.PAID.value,
        )
        self.session.add(self.order)
        await self.session.commit()
        self.pg = SimpleNamespace(
            create_user_from_template=AsyncMock(return_value={
                "id": 901,
                "username": "new_service",
                "subscription_url": "https://example.com/sub/new_service",
            }),
        )

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _prior(self, *, owner, remark):
        service = UserService(
            bot_user_id=owner.id,
            plan_id=self.plan.id,
            pg_user_id=900,
            pg_username="prior_service",
            remark=remark,
        )
        self.session.add(service)
        await self.session.commit()
        return service

    async def _deliver(self):
        with (
            patch("app.services.orders.get_pg", return_value=self.pg),
            patch("app.services.users.get_all_settings", AsyncMock(return_value={})),
            patch("app.services.loyalty.on_order_delivered", AsyncMock()),
        ):
            return await deliver_order(self.session, self.order)

    async def _assert_new_delivery(self, prior):
        result = await self._deliver()
        self.assertEqual(result.status, OrderStatus.DELIVERED.value)
        self.assertNotEqual(result.service_id, prior.id)
        created = await self.session.get(UserService, result.service_id)
        self.assertEqual(created.bot_user_id, self.buyer.id)
        self.assertEqual(created.remark, "order:12")
        self.pg.create_user_from_template.assert_awaited_once()

    async def test_order_prefix_does_not_reuse_another_buyers_service(self):
        prior = await self._prior(owner=self.other, remark="order:123")
        await self._assert_new_delivery(prior)

    async def test_order_prefix_does_not_reuse_same_buyers_other_order(self):
        prior = await self._prior(owner=self.buyer, remark="order:123")
        await self._assert_new_delivery(prior)

    async def test_exact_reference_still_requires_the_correct_buyer(self):
        prior = await self._prior(owner=self.other, remark="order:12")
        await self._assert_new_delivery(prior)

    async def test_exact_owned_service_is_recovered_without_provisioning_twice(self):
        prior = await self._prior(owner=self.buyer, remark="order:12")
        for _ in range(2):
            result = await self._deliver()
            self.assertEqual(result.status, OrderStatus.DELIVERED.value)
            self.assertEqual(result.service_id, prior.id)
        self.pg.create_user_from_template.assert_not_awaited()
        rows = list((await self.session.execute(select(UserService))).scalars())
        self.assertEqual([row.id for row in rows], [prior.id])
