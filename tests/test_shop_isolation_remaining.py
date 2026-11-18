"""Points metrics follow the purse; importing a service follows shop membership."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, BotUserShop, PointsTransaction, ResellerProfile
from app.services import service_import
from app.services.loyalty import overview_metrics
from app.services.service_import import ServiceImportError, _attach
from tests.postgres_support import scoped_async_engine

SHOP_ID, MEMBER, STRANGER = 5, 7, 8


class RemainingScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=MEMBER, telegram_id=107, referral_code="m", role="user"),
                BotUser(id=STRANGER, telegram_id=108, referral_code="s", role="user"),
            ])
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_ID, is_active=True))
            session.add(BotUserShop(bot_user_id=MEMBER, shop_id=SHOP_ID))
            session.add_all([
                PointsTransaction(user_id=MEMBER, amount=40, balance_after=40, tx_type="earn", idempotency_key="a", reseller_id=None),
                PointsTransaction(user_id=MEMBER, amount=15, balance_after=15, tx_type="earn", idempotency_key="b", reseller_id=SHOP_ID),
                PointsTransaction(user_id=MEMBER, amount=-5, balance_after=10, tx_type="redeem", idempotency_key="c", reseller_id=SHOP_ID),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_points_metrics_count_only_the_purse_of_each_shop(self):
        async with self.Session() as session:
            platform = await overview_metrics(session, reseller_id=None)
            shop = await overview_metrics(session, reseller_id=SHOP_ID)
        self.assertEqual((platform["points_issued"], platform["points_redeemed"]), (40, 0))
        self.assertEqual((shop["points_issued"], shop["points_redeemed"]), (15, 5))

    async def attach(self, user_id: int) -> AsyncMock:
        attached = AsyncMock()
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            with (
                patch.object(service_import, "verify_link", AsyncMock()),
                patch.object(service_import, "attach_verified", attached),
            ):
                await _attach(session, user, "link", SHOP_ID, True, None)
        return attached

    async def test_a_shop_member_without_attribution_can_add_a_service_in_that_shop(self):
        (await self.attach(MEMBER)).assert_awaited_once()

    async def test_a_stranger_cannot_add_a_service_in_a_shop(self):
        with self.assertRaises(ServiceImportError):
            await self.attach(STRANGER)
