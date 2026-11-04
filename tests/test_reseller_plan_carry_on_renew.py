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

    async def test_second_renew_stays_cleared_and_charges_again(self):
        panel = AsyncMock(
            return_value=SimpleNamespace(restored={"ok": 1, "errors": 0}, usage_reset=True)
        )
        with (
            patch("app.services.pg_admin_subscription._utcnow", return_value=NOW),
            patch("app.services.pg_admin_subscription._apply_renewal_on_panel", panel),
            patch(
                "app.services.volume_allocation_cascade.assert_child_growth_fits",
                AsyncMock(),
            ),
        ):
            await renew_subscription(
                self.session,
                sub=self.sub,
                plan=self.plan,
                payer=self.owner,
                charge_wallet=True,
            )
            await renew_subscription(
                self.session,
                sub=self.sub,
                plan=self.plan,
                payer=self.owner,
                charge_wallet=True,
            )
        await self.session.refresh(self.sub)
        await self.session.refresh(self.owner)
        self.assertEqual(self.sub.extra_gb_purchased, 0)
        self.assertEqual(self.sub.extra_users_purchased, 0)
        self.assertEqual(int(self.sub.renew_generation or 0), 2)
        self.assertEqual(self.owner.wallet_balance, 800_000)
        expires = self.sub.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        self.assertEqual(expires, NOW + timedelta(days=30))

    async def test_panel_capacity_sync_uses_base_after_extras_cleared(self):
        from app.services.pg_admin_subscription import _apply_renewal_on_panel, _extend_period

        _extend_period(self.sub, self.plan, NOW)
        self.assertEqual(self.sub.extra_gb_purchased, 0)
        self.assertEqual(self.sub.extra_users_purchased, 0)
        self.assertEqual(self.sub.base_gb, 100)
        self.assertEqual(self.sub.base_users, 10)

        sync = AsyncMock()
        restore = AsyncMock(return_value={"ok": 1, "errors": 0})
        with (
            patch(
                "app.services.pg_admin_subscription.sync_pg_capacity_from_sub",
                sync,
            ),
            patch(
                "app.services.pg_admin_subscription.restore_subscription_access",
                restore,
            ),
            patch(
                "app.services.pasarguard.get_pg",
                return_value=SimpleNamespace(reset_admin_usage=AsyncMock()),
            ),
        ):
            outcome = await _apply_renewal_on_panel(
                self.session, self.sub, [], refills_volume=True
            )
        sync.assert_awaited_once()
        called_sub = sync.await_args.args[1]
        self.assertEqual(
            int(called_sub.base_gb or 0) + int(called_sub.extra_gb_purchased or 0),
            100,
        )
        self.assertEqual(
            int(called_sub.base_users or 0) + int(called_sub.extra_users_purchased or 0),
            10,
        )
        self.assertTrue(outcome.usage_reset)

    async def test_current_capacity_renew_preserves_base_while_clearing_extras(self):
        from app.services.pg_admin_subscription import PLAN_KIND_CURRENT_CAPACITY

        self.plan.plan_kind = PLAN_KIND_CURRENT_CAPACITY
        self.plan.included_gb = 0
        self.plan.included_users = 0
        self.sub.base_gb = 250
        self.sub.base_users = 40
        await self.session.commit()

        panel = AsyncMock(
            return_value=SimpleNamespace(restored={"ok": 1, "errors": 0}, usage_reset=True)
        )
        with (
            patch("app.services.pg_admin_subscription._utcnow", return_value=NOW),
            patch("app.services.pg_admin_subscription._apply_renewal_on_panel", panel),
            patch(
                "app.services.volume_allocation_cascade.assert_child_growth_fits",
                AsyncMock(),
            ),
        ):
            await renew_subscription(
                self.session,
                sub=self.sub,
                plan=self.plan,
                payer=self.owner,
                charge_wallet=True,
            )
        await self.session.refresh(self.sub)
        self.assertEqual(self.sub.base_gb, 250)
        self.assertEqual(self.sub.base_users, 40)
        self.assertEqual(self.sub.extra_gb_purchased, 0)
        self.assertEqual(self.sub.extra_users_purchased, 0)

    async def test_renew_does_not_touch_another_reseller_subscription(self):
        other_owner = BotUser(
            id=9, telegram_id=9009, referral_code="own9", wallet_balance=500_000
        )
        other_plan = ResellerPlan(
            id=2,
            name="دیگر",
            price=50_000,
            renew_price=50_000,
            billing_mode="fixed",
            plan_kind="subscription",
            duration_days=30,
            included_gb=50,
            included_users=5,
            carry_remaining_period=True,
            keep_extras_on_renew=True,
            is_active=True,
        )
        other_sub = PgAdminSubscription(
            id=2,
            pg_username="shop9",
            plan_id=2,
            access_status="active",
            expires_at=NOW + timedelta(days=20),
            base_gb=50,
            base_users=5,
            extra_gb_purchased=7,
            extra_users_purchased=2,
            renew_generation=3,
        )
        self.session.add_all([other_owner, other_plan, other_sub])
        await self.session.commit()

        panel = AsyncMock(
            return_value=SimpleNamespace(restored={"ok": 1, "errors": 0}, usage_reset=True)
        )
        with (
            patch("app.services.pg_admin_subscription._utcnow", return_value=NOW),
            patch("app.services.pg_admin_subscription._apply_renewal_on_panel", panel),
            patch(
                "app.services.volume_allocation_cascade.assert_child_growth_fits",
                AsyncMock(),
            ),
        ):
            await renew_subscription(
                self.session,
                sub=self.sub,
                plan=self.plan,
                payer=self.owner,
                charge_wallet=True,
            )
        await self.session.refresh(other_sub)
        await self.session.refresh(other_owner)
        self.assertEqual(other_sub.extra_gb_purchased, 7)
        self.assertEqual(other_sub.extra_users_purchased, 2)
        self.assertEqual(int(other_sub.renew_generation or 0), 3)
        self.assertEqual(other_owner.wallet_balance, 500_000)
        expires = other_sub.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        self.assertEqual(expires, NOW + timedelta(days=20))

    async def test_renew_package_blocks_when_live_users_exceed_post_clear_cap(self):
        from app.db.models import ResellerProfile
        from app.services.reseller_package import AdminUsage, renew_package

        profile = ResellerProfile(
            id=1,
            user_id=3,
            plan_id=1,
            pg_admin_username="shop3",
            is_active=True,
        )
        self.session.add(profile)
        await self.session.commit()

        with (
            patch(
                "app.services.reseller_package.assert_shop_allows_renew",
                AsyncMock(),
            ),
            patch(
                "app.services.reseller_package.read_admin_usage",
                AsyncMock(return_value=AdminUsage(users=12, used_bytes=0)),
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                await renew_package(
                    self.session,
                    owner=self.owner,
                    profile=profile,
                    plan_id=1,
                    generation=0,
                )
        self.assertIn("کاربر", str(ctx.exception))
        await self.session.refresh(self.sub)
        await self.session.refresh(self.owner)
        self.assertEqual(self.sub.extra_gb_purchased, 25)
        self.assertEqual(self.owner.wallet_balance, 1_000_000)


if __name__ == "__main__":
    unittest.main()
