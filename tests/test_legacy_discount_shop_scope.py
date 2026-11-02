"""Legacy discount codes have no shop: they are the platform's and never discount a shop's order."""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, DiscountCode, Order
from app.services.orders import _reserve_discount_code, apply_discount
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

SHOP = 5


class LegacyDiscountShopScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=SHOP, telegram_id=5005, referral_code="o5", role="reseller"),
            BotUser(id=10, telegram_id=1010, referral_code="c10", role="user"),
            DiscountCode(id=1, code="LEGACY50", percent=50),
            Order(id=1, user_id=10, amount=1_000, reseller_id=SHOP),
            Order(id=2, user_id=10, amount=1_000),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def test_a_shop_order_is_not_discounted_and_uses_nothing(self) -> None:
        shop_order = await self.session.get(Order, 1)
        self.assertEqual(await _reserve_discount_code(self.session, "LEGACY50", 1_000, order=shop_order), (0, None))
        self.assertEqual((await self.session.get(DiscountCode, 1)).used_count, 0)

    async def test_the_shop_preview_shows_no_discount(self) -> None:
        token = set_shop_reseller_id(SHOP)
        try:
            self.assertEqual(await apply_discount(self.session, "LEGACY50", 1_000), (0, None))
        finally:
            reset_shop_reseller_id(token)

    async def test_a_platform_order_is_still_discounted(self) -> None:
        platform_order = await self.session.get(Order, 2)
        self.assertEqual(
            await _reserve_discount_code(self.session, "LEGACY50", 1_000, order=platform_order), (500, "LEGACY50")
        )
        self.assertEqual(await apply_discount(self.session, "LEGACY50", 1_000), (500, "LEGACY50"))


if __name__ == "__main__":
    unittest.main()
