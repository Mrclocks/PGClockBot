"""Recount of panel-only users' volume: paging, matching, failures and scheduling order."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, PgAdminSubscription, ResellerProfile, Role, UserService
from app.services.billing import GB
from app.services.volume_allocation import VolumeAllocationError, assert_volume_delta_allowed, sum_allocated_bytes
from app.services.volume_allocation_external import (
    IN_CHUNK,
    PAGE_SIZE,
    PanelListingIncomplete,
    compute_external_bytes,
    due_shop_ids,
    external_sync_is_stale,
    list_panel_users_for_admin,
    run_external_allocation_sync,
)

ROOT = Path(__file__).resolve().parents[1]


class ExternalHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'ext.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _shop(self, shop_id: int = 10, *, ceiling_gb: int = 10) -> ResellerProfile:
        async with self.Session() as session:
            session.add(
                BotUser(
                    id=shop_id,
                    telegram_id=1000 + shop_id,
                    role=Role.RESELLER.value,
                    referral_code=f"s{shop_id}",
                )
            )
            profile = ResellerProfile(
                user_id=shop_id,
                pg_admin_username=f"admin{shop_id}",
                billing_mode="fixed",
                allow_volume_oversell=False,
                is_active=True,
                external_allocated_bytes=0,
            )
            session.add(profile)
            session.add(
                PgAdminSubscription(
                    pg_username=f"admin{shop_id}",
                    base_gb=ceiling_gb,
                    extra_gb_purchased=0,
                )
            )
            await session.commit()
            await session.refresh(profile)
            return profile


def _paged(users: list[dict], *, total: int | None = None):
    async def get_users(*, admin, limit, offset):
        body = {"users": users[offset : offset + limit]}
        if total is not None:
            body["total"] = total
        return body

    return get_users


def _users(n: int, gb: int = 1, start: int = 1) -> list[dict]:
    return [{"id": i, "username": f"p{i}", "data_limit": gb * GB} for i in range(start, start + n)]


class PaginationAndMatchTests(ExternalHarness):
    async def test_every_page_is_collected(self) -> None:
        pg = AsyncMock(get_users=_paged(_users(PAGE_SIZE + 3), total=PAGE_SIZE + 3))
        self.assertEqual(len(await list_panel_users_for_admin(pg, "admin10")), PAGE_SIZE + 3)

    async def test_list_that_changes_while_paging_is_refused(self) -> None:
        pg = AsyncMock(get_users=_paged(_users(PAGE_SIZE + 3), total=PAGE_SIZE + 9))
        with self.assertRaises(PanelListingIncomplete):
            await list_panel_users_for_admin(pg, "admin10")

    async def test_panel_only_users_count_as_external(self) -> None:
        await self._shop()
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=1, role=Role.USER.value, referral_code="u1"))
            session.add_all(
                [
                    UserService(bot_user_id=1, pg_user_id=1, pg_username="local-one", shop_id=10,
                                quota_data_limit_bytes=GB),
                    # Saved before its panel id was known: matched by username.
                    UserService(bot_user_id=1, pg_user_id=None, pg_username="Local-Two", shop_id=10,
                                quota_data_limit_bytes=GB),
                    # Another shop's service never hides this shop's panel user.
                    UserService(bot_user_id=1, pg_user_id=99, pg_username="x", shop_id=20,
                                quota_data_limit_bytes=GB),
                ]
            )
            await session.commit()
            panel = [
                {"id": 1, "username": "local-one", "data_limit": GB},
                {"id": 2, "username": "local-two", "data_limit": GB},
                {"id": 99, "username": "panel-only", "data_limit": 4 * GB},
                {"id": 100, "username": "unlimited", "data_limit": 0},
            ]
            self.assertEqual(await compute_external_bytes(session, shop_id=10, panel_users=panel), 4 * GB)

    async def test_large_lists_are_matched_in_chunks(self) -> None:
        await self._shop()
        async with self.Session() as session:
            panel = _users(IN_CHUNK * 2 + 5)
            self.assertEqual(
                await compute_external_bytes(session, shop_id=10, panel_users=panel), (IN_CHUNK * 2 + 5) * GB
            )


class SyncBehaviourTests(ExternalHarness):
    async def _sync(self, pg_by_shop: dict[int, object]) -> list:
        async def client(session, shop_id):
            return pg_by_shop[shop_id]

        with patch("app.services.volume_allocation_external.get_pg_for_reseller", new=client):
            return await run_external_allocation_sync(self.Session)

    async def _external(self, shop_id: int) -> int:
        async with self.Session() as session:
            return int(
                await session.scalar(
                    select(ResellerProfile.external_allocated_bytes).where(ResellerProfile.user_id == shop_id)
                )
            )

    async def test_sync_stores_external_and_blocks_sell(self) -> None:
        await self._shop(ceiling_gb=5)
        await self._sync({10: AsyncMock(get_users=_paged(_users(1, gb=5, start=50)))})
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, 10), 5 * GB)
            with self.assertRaises(VolumeAllocationError):
                await assert_volume_delta_allowed(session, shop_id=10, delta_bytes=GB, audience="admin")

    async def test_panel_outage_keeps_the_previous_total(self) -> None:
        await self._shop(ceiling_gb=5)
        async with self.Session() as session:
            row = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 10))
            row.external_allocated_bytes = 5 * GB
            row.external_alloc_synced_at = datetime.now(timezone.utc) - timedelta(hours=2)
            await session.commit()
        results = await self._sync({10: AsyncMock(get_users=AsyncMock(side_effect=TimeoutError("panel down")))})
        self.assertIsNotNone(results[0].error)
        self.assertEqual(await self._external(10), 5 * GB)

    async def test_one_bad_shop_does_not_stop_others(self) -> None:
        await self._shop(shop_id=10)
        await self._shop(shop_id=20)
        results = await self._sync(
            {
                10: AsyncMock(get_users=AsyncMock(side_effect=RuntimeError("boom"))),
                20: AsyncMock(get_users=_paged(_users(1, gb=2, start=7))),
            }
        )
        by_shop = {r.shop_id: r for r in results}
        self.assertIsNotNone(by_shop[10].error)
        self.assertEqual(by_shop[20].external_bytes, 2 * GB)
        self.assertEqual(await self._external(20), 2 * GB)

    async def test_recently_counted_shops_wait_and_oldest_go_first(self) -> None:
        now = datetime.now(timezone.utc)
        for shop_id, synced in ((10, now), (20, now - timedelta(hours=3)), (30, None), (40, now - timedelta(hours=1))):
            await self._shop(shop_id=shop_id)
            async with self.Session() as session:
                row = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == shop_id))
                row.external_alloc_synced_at = synced
                await session.commit()
        async with self.Session() as session:
            self.assertEqual(await due_shop_ids(session, period_minutes=15), [30, 20, 40])
            self.assertEqual(await due_shop_ids(session, period_minutes=15, limit=1), [30])

    def test_stale_after_two_periods(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertTrue(external_sync_is_stale(now - timedelta(minutes=31), period_minutes=15, now=now))
        self.assertFalse(external_sync_is_stale(now - timedelta(minutes=20), period_minutes=15, now=now))
        self.assertTrue(external_sync_is_stale(None, period_minutes=15, now=now))


class MigrationTests(unittest.TestCase):
    def test_migration_upgrade_twice_and_downgrade(self) -> None:
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "ext_mig", ROOT / "alembic/versions/0066_external_volume_allocation.py"
        )
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE reseller_profiles ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "allow_volume_oversell BOOLEAN NOT NULL DEFAULT 1)"
                )
            )
            conn.execute(text("INSERT INTO reseller_profiles VALUES (1, 10, 1)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                row = conn.execute(
                    text(
                        "SELECT external_allocated_bytes, external_alloc_synced_at "
                        "FROM reseller_profiles"
                    )
                ).one()
                self.assertEqual(int(row[0]), 0)
                self.assertIsNone(row[1])
                migration.downgrade()
                cols = {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")}
                self.assertNotIn("external_allocated_bytes", cols)
                migration.upgrade()
                cols = {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")}
                self.assertIn("external_allocated_bytes", cols)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
