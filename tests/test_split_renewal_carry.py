"""Volume and time carry over separately on customer renewals; extra GB and extra users separately on reseller renewals."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Plan, ResellerPlan
from app.services.pg_admin_subscription import (
    EXTRA_GB,
    EXTRA_USERS,
    _extend_period,
    compute_renew_invoice,
    plan_keeps_extra,
)
from app.services.renewal_carry import (
    CARRY_TIME,
    CARRY_VOLUME,
    carry_status_text,
    plan_carries,
    set_plan_carry_from_form,
    toggle_plan_carry,
)
from app.services.reseller_package import EXTRA_USERS_CLEARED_WARN, renew_confirm_warnings_from_invoice
from app.services.service_renewals import calculate_renewal, renewal_terms

GB = 1024**3
DAY = 86400
NOW = 1_700_000_000
ROOT = Path(__file__).resolve().parents[1]
INFO = {"status": "active", "data_limit": 100 * GB, "used_traffic": 60 * GB, "expire": NOW + 10 * DAY}


def renew(*, volume: bool, time: bool) -> dict:
    plan = SimpleNamespace(data_limit_gb=50, duration_days=30, carry_remaining_volume=volume, carry_remaining_time=time)
    return calculate_renewal(INFO, renewal_terms(plan), now=NOW)


class CustomerCarryTests(unittest.TestCase):
    def test_volume_carried_time_restarted(self) -> None:
        result = renew(volume=True, time=False)
        self.assertEqual(result["result_remaining_bytes"], 90 * GB)
        self.assertEqual(result["payload"]["expire"], NOW + 30 * DAY)
        self.assertTrue(any("مانده زمان به دوره جدید منتقل نمی‌شود" in w for w in result["warnings"]))
        self.assertFalse(any("مانده حجم" in w for w in result["warnings"]))

    def test_time_carried_volume_restarted(self) -> None:
        result = renew(volume=False, time=True)
        self.assertEqual(result["result_remaining_bytes"], 50 * GB)
        self.assertEqual(result["payload"]["data_limit"], 110 * GB)
        self.assertEqual(result["payload"]["expire"], NOW + 40 * DAY)
        self.assertTrue(any("مانده حجم به دوره جدید منتقل نمی‌شود" in w for w in result["warnings"]))

    def test_both_carried_has_no_carry_warning(self) -> None:
        result = renew(volume=True, time=True)
        self.assertEqual((result["result_remaining_bytes"], result["payload"]["expire"]), (90 * GB, NOW + 40 * DAY))
        self.assertFalse(any("منتقل نمی‌شود" in w for w in result["warnings"]))

    def test_order_paid_before_the_split_keeps_its_single_flag(self) -> None:
        old_terms = {"version": 1, "volume_bytes": 50 * GB, "duration_seconds": 30 * DAY, "carry_remaining": False}
        result = calculate_renewal(INFO, old_terms, now=NOW)
        self.assertEqual((result["result_remaining_bytes"], result["payload"]["expire"]), (50 * GB, NOW + 30 * DAY))

    def test_terms_freeze_each_switch(self) -> None:
        terms = renewal_terms(SimpleNamespace(data_limit_gb=10, duration_days=30, carry_remaining_volume=False, carry_remaining_time=True))
        self.assertEqual((terms["carry_volume"], terms["carry_time"]), (False, True))

    def test_toggle_flips_only_one_part(self) -> None:
        plan = SimpleNamespace(carry_remaining_volume=True, carry_remaining_time=True)
        toggle_plan_carry(plan, CARRY_TIME)
        self.assertEqual((plan_carries(plan, CARRY_VOLUME), plan_carries(plan, CARRY_TIME)), (True, False))
        self.assertIn("زمان: منتقل نمی‌شود", carry_status_text(plan))
        with self.assertRaises(ValueError):
            toggle_plan_carry(plan, "users")

    def test_form_reads_both_switches(self) -> None:
        plan = SimpleNamespace()
        set_plan_carry_from_form(plan, {"carry_remaining_volume": "1"})
        self.assertEqual((plan.carry_remaining_volume, plan.carry_remaining_time), (True, False))


class ResellerExtrasTests(unittest.TestCase):
    def plan(self, *, gb: bool, users: bool) -> SimpleNamespace:
        return SimpleNamespace(
            id=3, renew_price=1000, price=1000, duration_days=30, included_gb=100, included_users=10,
            extra_gb_price=10, extra_user_price=100, renew_pricing_mode="from_capacity", allow_buy_extra=True,
            carry_remaining_period=True, keep_extra_gb_on_renew=gb, keep_extra_users_on_renew=users, plan_kind="subscription",
        )

    def sub(self) -> SimpleNamespace:
        return SimpleNamespace(
            base_gb=100, base_users=10, extra_gb_purchased=20, extra_users_purchased=5,
            expires_at=datetime.now(timezone.utc) + timedelta(days=3), access_status="active", plan_id=3,
            last_renewed_at=None, warn_sent_at=None, expired_at=None,
        )

    def test_keep_volume_clear_users(self) -> None:
        invoice = compute_renew_invoice(self.plan(gb=True, users=False), self.sub())
        self.assertEqual((invoice["extra_gb"], invoice["extra_users"]), (20, 0))
        self.assertEqual((invoice["cleared_extra_gb"], invoice["cleared_extra_users"]), (0, 5))
        self.assertEqual(invoice["total"], 1000 + 20 * 10)
        self.assertEqual(renew_confirm_warnings_from_invoice(self.plan(gb=True, users=False), invoice)[0], EXTRA_USERS_CLEARED_WARN)

    def test_renewal_clears_only_the_switched_off_kind(self) -> None:
        sub = self.sub()
        _extend_period(sub, self.plan(gb=False, users=True), datetime.now(timezone.utc))
        self.assertEqual((sub.extra_gb_purchased, sub.extra_users_purchased), (0, 5))

    def test_package_with_only_the_old_switch_uses_it_for_both(self) -> None:
        old = SimpleNamespace(keep_extras_on_renew=False)
        self.assertEqual((plan_keeps_extra(old, EXTRA_GB), plan_keeps_extra(old, EXTRA_USERS)), (False, False))


class LegacyInsertTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_created_with_the_old_switch_copy_it(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        engine = create_async_engine(f"sqlite+aiosqlite:///{Path(tmp.name) / 'c.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            plan = Plan(name="old", price=1, duration_days=30, carry_remaining_quota=False)
            fresh = Plan(name="new", price=1, duration_days=30)
            package = ResellerPlan(name="pkg", price=1, keep_extras_on_renew=False)
            session.add_all([plan, fresh, package])
            await session.commit()
            self.assertEqual((plan.carry_remaining_volume, plan.carry_remaining_time), (False, False))
            self.assertEqual((fresh.carry_remaining_volume, fresh.carry_remaining_time), (True, True))
            self.assertEqual((package.keep_extra_gb_on_renew, package.keep_extra_users_on_renew), (False, False))
        await engine.dispose()
        tmp.cleanup()


class SplitMigrationTests(unittest.TestCase):
    def test_upgrade_copies_the_old_switch_runs_twice_and_downgrades(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("split_carry", ROOT / "alembic/versions/0119_split_renewal_carry.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE plans (id INTEGER PRIMARY KEY, carry_remaining_quota BOOLEAN)"))
            conn.execute(text("CREATE TABLE reseller_plans (id INTEGER PRIMARY KEY, keep_extras_on_renew BOOLEAN)"))
            conn.execute(text("INSERT INTO plans VALUES (1, 0), (2, 1)"))
            conn.execute(text("INSERT INTO reseller_plans VALUES (1, 0)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                conn.execute(text("UPDATE plans SET carry_remaining_time = 1 WHERE id = 1"))
                migration.upgrade()
                self.assertEqual(
                    conn.execute(text("SELECT carry_remaining_volume, carry_remaining_time FROM plans ORDER BY id")).all(),
                    [(0, 1), (1, 1)],
                    "a second upgrade does not copy over switches set after the first",
                )
                self.assertEqual(
                    conn.execute(text("SELECT keep_extra_gb_on_renew, keep_extra_users_on_renew FROM reseller_plans")).one(),
                    (0, 0),
                )
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT count(*) FROM plans")).scalar(), 2)
                self.assertNotIn("carry_remaining_volume", {c["name"] for c in sa.inspect(conn).get_columns("plans")})
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
