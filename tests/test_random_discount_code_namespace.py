"""Gift codes cannot shadow a random discount issued by the same shop."""
from __future__ import annotations

import secrets
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, ChargeCode, DiscountCode, Plan
from app.services.gift_codes import create_gift_code
from app.services.orders import apply_discount_to_order, cancel_order, create_order
from app.services.random_discounts import RandomDiscountInput, create_random_discount, stop_random_discount
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id

SHOP = {"role": "reseller", "bot_user_id": 2, "permissions": ["random_discounts"]}


class RandomDiscountCodeNamespaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.token = set_shop_reseller_id(2)
        _SETTINGS_CACHE.clear()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="buyer"),
            BotUser(id=2, telegram_id=102, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=103, referral_code="shop3", role="reseller"),
            Plan(id=1, name="پلن", price=1000, owner_reseller_id=2),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()

    def data(self, code: str) -> RandomDiscountInput:
        return RandomDiscountInput(
            title="تخفیف تصادفی", mode="code", code=code, reduction_kind="amount", minimum=10,
            maximum=20, step=1, window_minutes=60, request_key=secrets.token_hex(16),
        )

    async def test_gift_code_cannot_shadow_existing_random_code_in_same_shop(self) -> None:
        policy = await create_random_discount(self.session, SHOP, self.data("SURPRISE"))
        with self.assertRaises(ValueError):
            await create_gift_code(self.session, code="surprise", reseller_id=2, kind="wallet", amount=100)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(ChargeCode)), 0)
        await self.session.refresh(policy)
        self.assertTrue(policy.is_active)

    async def test_stopped_random_code_name_remains_reserved_in_issuing_shop(self) -> None:
        policy = await create_random_discount(self.session, SHOP, self.data("SURPRISE"))
        await stop_random_discount(self.session, SHOP, policy.id)
        with self.assertRaises(ValueError):
            await create_gift_code(self.session, code="SURPRISE", reseller_id=2, kind="discount", percent=10)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(ChargeCode)), 0)

    async def test_random_code_in_other_shop_does_not_reserve_gift_namespace_here(self) -> None:
        await create_random_discount(self.session, {**SHOP, "bot_user_id": 3}, self.data("SURPRISE"))
        gift = await create_gift_code(self.session, code="SURPRISE", reseller_id=2, kind="wallet", amount=100)
        self.assertEqual((gift.reseller_id, gift.code, gift.used_count), (2, "SURPRISE", 0))

    async def test_random_policy_cannot_shadow_existing_gift_code(self) -> None:
        gift = await create_gift_code(self.session, code="SURPRISE", reseller_id=2, kind="discount", percent=10)
        with self.assertRaises(ValueError):
            await create_random_discount(self.session, SHOP, self.data("SURPRISE"))
        await self.session.refresh(gift)
        self.assertEqual(gift.used_count, 0)

    async def test_cancelling_random_order_does_not_release_unrelated_legacy_code(self) -> None:
        await create_random_discount(self.session, SHOP, self.data("SURPRISE"))
        with patch("app.services.orders.require_purchase_contact", AsyncMock()):
            order = await create_order(self.session, user_id=1, plan_id=1)
        await apply_discount_to_order(self.session, order, "SURPRISE")
        legacy = DiscountCode(code="SURPRISE", percent=10, used_count=3)
        self.session.add(legacy)
        await self.session.commit()
        await cancel_order(self.session, order)
        await self.session.refresh(legacy)
        self.assertEqual(legacy.used_count, 3)
