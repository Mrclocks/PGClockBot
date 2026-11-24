"""A payment button only works in the bot whose shop placed the order."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import keyboards  # noqa: F401  (import order: breaks a handler import cycle)
from app.bot.handlers.shop import pay_back
from app.db import Base
from app.db.models import BotUser, Order, OrderStatus
from app.services.orders import is_customer_order_in_current_shop
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine

SHOP_ID = 5
PLATFORM_ORDER, SHOP_ORDER, OTHER_USER_ORDER = 1, 2, 3


class OrderShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=7, telegram_id=107, referral_code="cust", role="user"),
                BotUser(id=8, telegram_id=108, referral_code="other", role="user"),
            ])
            await session.flush()
            for order_id, user_id, shop in (
                (PLATFORM_ORDER, 7, None), (SHOP_ORDER, 7, SHOP_ID), (OTHER_USER_ORDER, 8, None),
            ):
                session.add(Order(
                    id=order_id, user_id=user_id, reseller_id=shop, amount=1000,
                    status=OrderStatus.PENDING.value,
                ))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def in_shop(self, order_id: int, shop: int | None, user_id: int = 7) -> bool:
        token = set_shop_reseller_id(shop)
        try:
            async with self.Session() as session:
                return is_customer_order_in_current_shop(await session.get(Order, order_id), user_id)
        finally:
            reset_shop_reseller_id(token)

    async def test_a_platform_order_is_usable_only_in_the_platform_bot(self):
        self.assertTrue(await self.in_shop(PLATFORM_ORDER, None))
        self.assertFalse(await self.in_shop(PLATFORM_ORDER, SHOP_ID))

    async def test_a_shop_order_is_usable_only_in_that_shops_bot(self):
        self.assertTrue(await self.in_shop(SHOP_ORDER, SHOP_ID))
        self.assertFalse(await self.in_shop(SHOP_ORDER, None))
        self.assertFalse(await self.in_shop(SHOP_ORDER, 99))

    async def test_another_customers_order_is_never_usable(self):
        self.assertFalse(await self.in_shop(OTHER_USER_ORDER, None))

    async def test_a_missing_order_is_refused(self):
        self.assertFalse(is_customer_order_in_current_shop(None, 7))

    async def test_the_payment_back_button_refuses_a_platform_order_inside_a_shop_bot(self):
        callback = MagicMock(data=f"pay:back:{PLATFORM_ORDER}", message=MagicMock())
        callback.answer = AsyncMock()
        token = set_shop_reseller_id(SHOP_ID)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, 7)
                await pay_back(callback, session, customer, state=MagicMock(), is_reseller_bot=True,
                               reseller_owner_id=SHOP_ID)
        finally:
            reset_shop_reseller_id(token)
        callback.answer.assert_awaited_once()
        self.assertEqual(callback.answer.await_args.args[0], "این سفارش دیگر در دسترس نیست")
