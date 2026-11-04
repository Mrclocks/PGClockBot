"""Reseller package switches for leftover period and purchased extras on renew."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan
from app.services.pg_admin_subscription import (
    compute_renew_invoice,
    plan_carries_remaining_period,
    plan_keeps_extras_on_renew,
    renew_subscription,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


class ResellerCarryHelpersTests(unittest.TestCase):
    def test_defaults_keep_previous_behavior(self):
        plan = SimpleNamespace()
        self.assertTrue(plan_carries_remaining_period(plan))
        self.assertTrue(plan_keeps_extras_on_renew(plan))

    def test_invoice_skips_extras_when_plan_clears_them(self):
        plan = SimpleNamespace(
            renew_price=100_000,
            price=100_000,
            allow_buy_extra=True,
            renew_pricing_mode="from_capacity",
            extra_gb_price=1_000,
            extra_user_price=2_000,
            duration_days=30,
            keep_extras_on_renew=False,
            carry_remaining_period=True,
        )
        sub = SimpleNamespace(
            extra_gb_purchased=10,
            extra_users_purchased=5,
            base_gb=100,
            base_users=20,
            expires_at=NOW + timedelta(days=5),
            access_status="active",
        )
        invoice = compute_renew_invoice(plan, sub)
        self.assertEqual(invoice["total"], 100_000)
        self.assertEqual(invoice["extra_gb"], 0)
        self.assertEqual(invoice["cleared_extra_gb"], 10)
        self.assertEqual(invoice["cleared_extra_users"], 5)


class ResellerCarryMigrationTests(unittest.TestCase):
    def test_sqlite_upgrade_repeat_and_downgrade_keep_plans(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "reseller_carry_mig",
            ROOT / "alembic/versions/0087_reseller_plan_carry_on_renew.py",
        )
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            conn.execute(
                sa.text(
                    "CREATE TABLE reseller_plans (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL)"
                )
            )
            conn.execute(sa.text("INSERT INTO reseller_plans VALUES (1, 'برنز')"))
            migration.upgrade()
            migration.upgrade()
            cols = {c["name"] for c in sa.inspect(conn).get_columns("reseller_plans")}
            self.assertIn("carry_remaining_period", cols)
            self.assertIn("keep_extras_on_renew", cols)
            row = conn.execute(
                sa.text(
                    "SELECT carry_remaining_period, keep_extras_on_renew FROM reseller_plans WHERE id = 1"
                )
            ).one()
            self.assertEqual(tuple(row), (1, 1))
            migration.downgrade()
            cols = {c["name"] for c in sa.inspect(conn).get_columns("reseller_plans")}
            self.assertNotIn("carry_remaining_period", cols)
            self.assertEqual(
                conn.execute(sa.text("SELECT name FROM reseller_plans WHERE id = 1")).scalar(),
                "برنز",
            )
        engine.dispose()


class ResellerCarryRenewalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'r.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.owner = BotUser(id=3, telegram_id=3003, referral_code="own", wallet_balance=1_000_000)
        self.plan = ResellerPlan(
            id=1,
            name="ماهانه",
            price=100_000,
            renew_price=100_000,
            billing_mode="fixed",
            plan_kind="subscription",
            duration_days=30,
            included_gb=100,
            included_users=10,
            allow_buy_extra=True,
            extra_gb_price=1_000,
            renew_pricing_mode="from_capacity",
            carry_remaining_period=False,
            keep_extras_on_renew=False,
            is_active=True,
        )
        self.sub = PgAdminSubscription(
            id=1,
            pg_username="shop3",
            plan_id=1,
            access_status="active",
            expires_at=NOW + timedelta(days=10),
            base_gb=100,
            base_users=10,
            extra_gb_purchased=25,
            extra_users_purchased=3,
            renew_generation=0,
        )
        self.session.add_all([self.owner, self.plan, self.sub])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_renew_starts_period_from_now_and_clears_extras(self):
        panel = AsyncMock(
            return_value=SimpleNamespace(restored={"ok": 1, "errors": 0}, usage_reset=True)
        )
        with (
            patch("app.services.pg_admin_subscription._utcnow", return_value=NOW),
            patch(
                "app.services.pg_admin_subscription._apply_renewal_on_panel",
                panel,
            ),
            patch(
                "app.services.volume_allocation_cascade.assert_child_growth_fits",
                AsyncMock(),
            ),
        ):
            result = await renew_subscription(
                self.session,
                sub=self.sub,
                plan=self.plan,
                payer=self.owner,
                charge_wallet=True,
            )
        await self.session.refresh(self.sub)
        await self.session.refresh(self.owner)
        expires = self.sub.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        self.assertEqual(expires, NOW + timedelta(days=30))
        self.assertEqual(self.sub.extra_gb_purchased, 0)
        self.assertEqual(self.sub.extra_users_purchased, 0)
        self.assertEqual(result["amount"], 100_000)
        self.assertEqual(self.owner.wallet_balance, 900_000)

    async def test_refused_panel_renew_restores_cleared_extras(self):
        from app.services.pg_admin_subscription import RENEW_REFUSED, _RenewRefused

        async def refuse(*_a, **_k):
            raise _RenewRefused()

        with (
            patch("app.services.pg_admin_subscription._utcnow", return_value=NOW),
            patch(
                "app.services.pg_admin_subscription._apply_renewal_on_panel",
                AsyncMock(side_effect=refuse),
            ),
            patch(
                "app.services.volume_allocation_cascade.assert_child_growth_fits",
                AsyncMock(),
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                await renew_subscription(
                    self.session,
                    sub=self.sub,
                    plan=self.plan,
                    payer=self.owner,
                    charge_wallet=True,
                )
        self.assertEqual(str(ctx.exception), RENEW_REFUSED)
        await self.session.refresh(self.sub)
        await self.session.refresh(self.owner)
        self.assertEqual(self.sub.extra_gb_purchased, 25)
        self.assertEqual(self.sub.extra_users_purchased, 3)
        self.assertEqual(self.owner.wallet_balance, 1_000_000)

    async def test_usage_blocker_when_cleared_extras_leave_shop_over_capacity(self):
        from app.services.reseller_package import AdminUsage, usage_blocker

        blocked = usage_blocker(
            self.plan,
            self.sub,
            AdminUsage(users=12, used_bytes=0),
        )
        self.assertIsNotNone(blocked)
        self.assertIn("کاربر", blocked or "")

    def test_confirm_warnings_mention_cleared_extras(self):
        from app.services.reseller_package import (
            EXTRAS_CLEARED_WARN,
            renew_confirm_warnings_from_invoice,
        )

        invoice = compute_renew_invoice(self.plan, self.sub)
        warnings = renew_confirm_warnings_from_invoice(self.plan, invoice)
        self.assertIn(EXTRAS_CLEARED_WARN, warnings)
        self.assertTrue(any("زمان" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
