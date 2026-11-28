"""«سرویس‌های من» drops services the panel removed after they ran out, one day after the removal."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, UserService
from app.services import service_panel_presence as presence
from app.services.service_scope import list_customer_services, page_customer_services, search_customer_services
from tests.postgres_support import scoped_async_engine

NOW = datetime.now(timezone.utc)
OLD_REMOVAL = NOW - timedelta(days=2)
RECENT_REMOVAL = NOW - timedelta(hours=2)


class RemovedServiceListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            customer = BotUser(telegram_id=1, referral_code="r1")
            shop_owner = BotUser(telegram_id=2, referral_code="r2")
            other_customer = BotUser(telegram_id=3, referral_code="r3")
            session.add_all([customer, shop_owner, other_customer])
            await session.flush()
            self.customer_id, self.shop_id, self.other_id = customer.id, shop_owner.id, other_customer.id
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def add(self, name: str, *, user_id: int | None = None, shop_id: int | None = None, **fields: object) -> int:
        async with self.Session() as session:
            service = UserService(
                bot_user_id=user_id or self.customer_id, pg_user_id=1, pg_username=name, shop_id=shop_id, **fields
            )
            session.add(service)
            await session.commit()
            return int(service.id)

    async def listed(self, *, user_id: int | None = None, shop_id: int | None = None) -> set[str]:
        async with self.Session() as session:
            rows = await list_customer_services(session, user_id or self.customer_id, shop_id, listed_only=True)
        return {row.pg_username for row in rows}

    async def test_a_service_removed_a_day_after_it_expired_leaves_the_list(self) -> None:
        await self.add("expired", quota_expire_at=OLD_REMOVAL - timedelta(days=1), panel_missing_at=OLD_REMOVAL)
        await self.add("active", quota_expire_at=NOW + timedelta(days=10))
        self.assertEqual(await self.listed(), {"active"})

    async def test_a_service_whose_volume_ran_out_leaves_the_list(self) -> None:
        await self.add(
            "used-up", quota_expire_at=NOW + timedelta(days=10),
            volume_exhausted_at=OLD_REMOVAL - timedelta(hours=1), panel_missing_at=OLD_REMOVAL,
        )
        self.assertEqual(await self.listed(), set())

    async def test_a_recent_removal_stays_listed_for_a_day(self) -> None:
        await self.add("recent", quota_expire_at=RECENT_REMOVAL - timedelta(days=1), panel_missing_at=RECENT_REMOVAL)
        self.assertEqual(await self.listed(), {"recent"})

    async def test_a_service_removed_while_still_usable_stays_listed(self) -> None:
        await self.add("owed", quota_expire_at=NOW + timedelta(days=5), panel_missing_at=OLD_REMOVAL)
        self.assertEqual(await self.listed(), {"owed"})

    async def test_a_service_without_a_cached_expiry_stays_listed(self) -> None:
        await self.add("unknown", panel_missing_at=OLD_REMOVAL)
        self.assertEqual(await self.listed(), {"unknown"})

    async def test_ownership_checks_still_count_a_hidden_service(self) -> None:
        await self.add("expired", quota_expire_at=OLD_REMOVAL - timedelta(days=1), panel_missing_at=OLD_REMOVAL)
        async with self.Session() as session:
            owned = await list_customer_services(session, self.customer_id, None, limit=1)
        self.assertEqual([row.pg_username for row in owned], ["expired"])

    async def test_search_and_mini_app_pages_hide_the_same_services(self) -> None:
        await self.add("vip-expired", quota_expire_at=OLD_REMOVAL - timedelta(days=1), panel_missing_at=OLD_REMOVAL)
        await self.add("vip-active", quota_expire_at=NOW + timedelta(days=10))
        async with self.Session() as session:
            found = await search_customer_services(session, self.customer_id, None, "vip", limit=10)
            page, has_more = await page_customer_services(session, self.customer_id, None, limit=10)
        self.assertEqual([row.pg_username for row in found], ["vip-active"])
        self.assertEqual(([row.pg_username for row in page], has_more), (["vip-active"], False))

    async def test_a_service_the_panel_reports_again_comes_back(self) -> None:
        service_id = await self.add(
            "restored", quota_expire_at=OLD_REMOVAL - timedelta(days=1), panel_missing_at=OLD_REMOVAL
        )
        self.assertEqual(await self.listed(), set())
        async with self.Session() as session:
            await presence.record_presence(session, service_id, presence.PRESENT)
        self.assertEqual(await self.listed(), {"restored"})

    async def test_the_list_stays_scoped_to_the_shop_and_the_customer(self) -> None:
        await self.add("platform", quota_expire_at=NOW + timedelta(days=10))
        await self.add("shop-sold", shop_id=self.shop_id, quota_expire_at=NOW + timedelta(days=10))
        await self.add("someone-else", user_id=self.other_id, quota_expire_at=NOW + timedelta(days=10))
        self.assertEqual(await self.listed(), {"platform"})
        self.assertEqual(await self.listed(shop_id=self.shop_id), {"shop-sold"})
        self.assertEqual(await self.listed(user_id=self.other_id), {"someone-else"})


if __name__ == "__main__":
    unittest.main()
