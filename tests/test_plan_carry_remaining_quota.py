"""Owner switch: whether leftover volume/time carries into the next renewal period."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import reseller_plans as reseller_plan_handlers
from app.db.models import Base, BotUser, Plan, ResellerProfile
from app.services.service_renewals import calculate_renewal, renewal_terms
from app.services.ux20 import clone_plan

ROOT = Path(__file__).resolve().parents[1]
GB = 1024**3
DAY = 86400


class CarryRemainingArithmeticTests(unittest.TestCase):
    def test_missing_carry_key_on_legacy_snapshot_keeps_old_behavior(self):
        result = calculate_renewal(
            {
                "status": "active",
                "data_limit": 10 * GB,
                "used_traffic": 3 * GB,
                "expire": 1_700_000_000 + 5 * DAY,
            },
            {
                "version": 1,
                "volume_bytes": 10 * GB,
                "duration_seconds": 30 * DAY,
            },
            now=1_700_000_000,
        )
        self.assertEqual(result["payload"]["data_limit"], 20 * GB)
        self.assertEqual(result["payload"]["expire"], 1_700_000_000 + 35 * DAY)

    def test_downgrade_without_carry_does_not_keep_previous_leftover(self):
        terms = renewal_terms(
            SimpleNamespace(data_limit_gb=20, duration_days=30, carry_remaining_quota=False)
        )
        result = calculate_renewal(
            {
                "status": "active",
                "data_limit": 50 * GB,
                "used_traffic": 5 * GB,
                "expire": 1_700_000_000 + 20 * DAY,
            },
            terms,
            now=1_700_000_000,
        )
        self.assertEqual(result["result_remaining_bytes"], 20 * GB)
        self.assertEqual(result["payload"]["data_limit"], 25 * GB)
        self.assertEqual(result["result_seconds"], 30 * DAY)


class CarryRemainingMigrationTests(unittest.TestCase):
    def test_sqlite_upgrade_repeat_and_downgrade_keep_plans(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "carry_mig", ROOT / "alembic/versions/0084_plan_carry_remaining_quota.py"
        )
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            conn.execute(
                sa.text(
                    "CREATE TABLE plans (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, price INTEGER NOT NULL)"
                )
            )
            conn.execute(sa.text("INSERT INTO plans VALUES (1, 'ماهانه', 100000)"))
            migration.upgrade()
            migration.upgrade()
            cols = {c["name"] for c in sa.inspect(conn).get_columns("plans")}
            self.assertIn("carry_remaining_quota", cols)
            self.assertEqual(
                conn.execute(sa.text("SELECT carry_remaining_quota FROM plans WHERE id = 1")).scalar(),
                1,
            )
            migration.downgrade()
            cols = {c["name"] for c in sa.inspect(conn).get_columns("plans")}
            self.assertNotIn("carry_remaining_quota", cols)
            self.assertEqual(conn.execute(sa.text("SELECT name FROM plans WHERE id = 1")).scalar(), "ماهانه")
        engine.dispose()


class CarryRemainingCloneAndShopIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'carry.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all(
            [
                BotUser(id=2, telegram_id=2002, referral_code="shop2", role="reseller"),
                BotUser(id=3, telegram_id=2003, referral_code="shop3", role="reseller"),
            ]
        )
        await self.session.flush()
        self.session.add_all(
            [
                ResellerProfile(id=1, user_id=2, is_active=True, bot_permissions="plans"),
                ResellerProfile(id=2, user_id=3, is_active=True, bot_permissions="plans"),
                Plan(
                    id=10,
                    name="فروشگاه ۲",
                    price=100_000,
                    duration_days=30,
                    data_limit_gb=40,
                    owner_reseller_id=2,
                    carry_remaining_quota=False,
                    is_active=True,
                ),
                Plan(
                    id=11,
                    name="فروشگاه ۳",
                    price=100_000,
                    duration_days=30,
                    data_limit_gb=40,
                    owner_reseller_id=3,
                    carry_remaining_quota=True,
                    is_active=True,
                ),
            ]
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_clone_copies_carry_flag(self):
        copy = await clone_plan(self.session, 10, owner_reseller_id=2)
        self.assertFalse(copy.carry_remaining_quota)
        self.assertFalse(copy.is_active)

    async def test_other_shop_cannot_toggle_carry_on_foreign_plan(self):
        profile = await self.session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 3))
        callback = MagicMock(
            data="res:plan:carry:10",
            answer=AsyncMock(),
            message=MagicMock(),
        )
        with (
            patch.object(reseller_plan_handlers, "_actor", AsyncMock(return_value=(3, profile))),
            patch.object(reseller_plan_handlers, "has_bot_perm", return_value=True),
            patch.object(reseller_plan_handlers, "safe_edit_text", AsyncMock()),
        ):
            await reseller_plan_handlers.res_plan_carry_toggle(
                callback, self.session, SimpleNamespace(id=3)
            )
        foreign = await self.session.get(Plan, 10)
        self.assertFalse(foreign.carry_remaining_quota)
        callback.answer.assert_awaited()
        alert_kwargs = callback.answer.await_args.kwargs
        self.assertTrue(alert_kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
