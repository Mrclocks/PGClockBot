"""Campaigns to a shop's whole audience: who is reached, and one per shop per day."""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.campaigns import BROAD_LIMIT_MESSAGE, audience_query, create_campaign

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 81
SHOP_B = 82


class BroadAudienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import BotUser, BotUserShop, MarketingPreference, UserService

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'camp.db'}", connect_args={"timeout": 15}
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller"))
            await session.flush()
            session.add_all([
                BotUser(id=101, telegram_id=1010, referral_code="c101", reseller_id=SHOP_A),  # active service in A
                BotUser(id=102, telegram_id=1020, referral_code="c102", reseller_id=SHOP_A),  # no service
                BotUser(id=103, telegram_id=1030, referral_code="c103", reseller_id=SHOP_B),  # B only
                BotUser(id=104, telegram_id=1040, referral_code="c104", reseller_id=SHOP_A),  # opted out of A
                BotUser(id=105, telegram_id=1050, referral_code="c105", reseller_id=SHOP_A),  # blocked in A
                BotUser(id=106, telegram_id=1060, referral_code="c106", reseller_id=SHOP_A, is_blocked=True),
                BotUser(id=107, telegram_id=1070, referral_code="c107", reseller_id=SHOP_B),  # active service in B
            ])
            await session.flush()
            session.add(MarketingPreference(user_id=104, shop_key=SHOP_A, enabled=False))
            session.add(BotUserShop(bot_user_id=105, shop_id=SHOP_A, is_blocked=True))
            for service_id, user_id, shop in ((1, 101, SHOP_A), (2, 107, SHOP_B)):
                session.add(UserService(
                    id=service_id, bot_user_id=user_id, shop_id=shop, pg_username=f"u{service_id}",
                    quota_status="active", quota_synced_at=now, quota_expire_at=now + timedelta(days=40),
                ))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _ids(self, shop_id, audience) -> set[int]:
        async with self.Session() as session:
            return set((await session.scalars(audience_query(shop_id=shop_id, audience=audience, days=7))).all())

    async def _create(self, shop_id, audience):
        async with self.Session() as session:
            return await create_campaign(session, shop_id=shop_id, audience=audience, days=7, text="hello", actor="t")

    async def _campaigns(self) -> int:
        from app.db.models import TargetedCampaign

        async with self.Session() as session:
            return int((await session.execute(select(func.count()).select_from(TargetedCampaign))).scalar_one())

    async def test_all_reaches_reachable_customers_of_this_shop_only(self):
        self.assertEqual(await self._ids(SHOP_A, "all"), {101, 102})
        self.assertEqual(await self._ids(SHOP_B, "all"), {103, 107})

    async def test_active_needs_an_active_service_in_this_shop(self):
        self.assertEqual(await self._ids(SHOP_A, "active"), {101})
        self.assertEqual(await self._ids(SHOP_B, "active"), {107})

    async def test_one_broad_campaign_per_shop_per_day(self):
        await self._create(SHOP_A, "all")
        for audience in ("all", "active"):
            with self.subTest(audience=audience), self.assertRaises(ValueError) as caught:
                await self._create(SHOP_A, audience)
            self.assertEqual(str(caught.exception), BROAD_LIMIT_MESSAGE)
        await self._create(SHOP_B, "active")
        self.assertEqual(await self._campaigns(), 2)

    async def test_concurrent_broad_campaigns_create_one(self):
        results = await asyncio.gather(*(self._create(SHOP_A, "all") for _ in range(4)), return_exceptions=True)
        self.assertEqual(sum(1 for r in results if not isinstance(r, Exception)), 1)
        self.assertEqual(await self._campaigns(), 1)

    async def test_an_empty_audience_does_not_use_the_days_campaign(self):
        from app.db.models import UserService

        async with self.Session() as session:
            await session.execute(sa.delete(UserService).where(UserService.shop_id == SHOP_A))
            await session.commit()
        with self.assertRaises(ValueError):
            await self._create(SHOP_A, "active")
        await self._create(SHOP_A, "all")


class BroadCampaignMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("broad_days", ROOT / "alembic/versions/0082_shop_broad_campaign_days.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
            self.assertIn("shop_broad_campaign_days", sa.inspect(conn).get_table_names())
            migration.downgrade()
            migration.downgrade()
            self.assertNotIn("shop_broad_campaign_days", sa.inspect(conn).get_table_names())
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
