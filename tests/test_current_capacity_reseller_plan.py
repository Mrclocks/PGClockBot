"""System reseller plan «ظرفیت فعلی» (current_capacity): seed, grant, renew safety."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


class PlanKindCurrentCapacityTests(unittest.TestCase):
    def test_helpers_treat_current_capacity_as_clock_plan(self):
        from app.services.pg_admin_subscription import (
            PLAN_KIND_CURRENT_CAPACITY,
            is_addon_plan,
            is_current_capacity_plan,
            is_subscription_plan,
            plan_kind_of,
        )

        plan = SimpleNamespace(plan_kind=PLAN_KIND_CURRENT_CAPACITY)
        self.assertEqual(plan_kind_of(plan), PLAN_KIND_CURRENT_CAPACITY)
        self.assertTrue(is_current_capacity_plan(plan))
        self.assertTrue(is_subscription_plan(plan))
        self.assertFalse(is_addon_plan(plan))

    def test_model_has_is_system(self):
        from app.db.models import ResellerPlan

        self.assertTrue(hasattr(ResellerPlan, "is_system"))
        self.assertTrue(hasattr(ResellerPlan, "plan_kind"))

    def test_alembic_revision_exists(self):
        path = ROOT / "alembic/versions/0043_reseller_plan_is_system.py"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("is_system", text)
        self.assertIn("0042_pg_api_key_enc", text)


class EnsureCurrentCapacityPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_creates_singleton_system_plan(self):
        from app.services.resellers import (
            CURRENT_CAPACITY_PLAN_KIND,
            CURRENT_CAPACITY_PLAN_NAME,
            ensure_current_capacity_reseller_plan,
        )

        created = []

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_Result())
        session.flush = AsyncMock()

        def _add(obj):
            created.append(obj)

        session.add = _add

        plan = await ensure_current_capacity_reseller_plan(session)
        self.assertEqual(len(created), 1)
        self.assertIs(plan, created[0])
        self.assertEqual(plan.name, CURRENT_CAPACITY_PLAN_NAME)
        self.assertEqual(plan.plan_kind, CURRENT_CAPACITY_PLAN_KIND)
        self.assertTrue(plan.is_system)
        self.assertTrue(plan.is_active)
        self.assertFalse(plan.create_pg_admin)
        self.assertEqual(plan.included_gb, 0)
        self.assertEqual(plan.included_users, 0)
        self.assertEqual(plan.billing_mode, "fixed")

    async def test_ensure_heals_existing_row(self):
        from app.services.resellers import (
            CURRENT_CAPACITY_PLAN_NAME,
            ensure_current_capacity_reseller_plan,
        )

        existing = SimpleNamespace(
            id=3,
            name="old",
            plan_kind="current_capacity",
            is_system=False,
            is_active=False,
            create_pg_admin=True,
            included_gb=50,
            included_users=10,
            billing_mode="payg",
        )

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return [existing]

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_Result())
        session.flush = AsyncMock()
        session.add = AsyncMock()

        plan = await ensure_current_capacity_reseller_plan(session)
        self.assertIs(plan, existing)
        self.assertTrue(plan.is_system)
        self.assertTrue(plan.is_active)
        self.assertFalse(plan.create_pg_admin)
        self.assertEqual(plan.included_gb, 0)
        self.assertEqual(plan.included_users, 0)
        self.assertEqual(plan.billing_mode, "fixed")
        self.assertEqual(plan.name, CURRENT_CAPACITY_PLAN_NAME)
        session.add.assert_not_called()


class DeleteSystemPlanBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_reseller_plan_blocks_system(self):
        from app.services.plans_catalog import PlanDeleteBlocked, delete_reseller_plan

        session = AsyncMock()
        plan = SimpleNamespace(id=1, name="ظرفیت فعلی", is_system=True)
        with self.assertRaises(PlanDeleteBlocked) as ctx:
            await delete_reseller_plan(session, plan)
        self.assertIn("سیستمی", ctx.exception.message)


class StartOrRefreshCurrentCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshots_live_pg_into_base_without_pushing_zeros(self):
        from app.services.pg_admin_subscription import start_or_refresh_subscription

        plan = SimpleNamespace(
            id=7,
            plan_kind="current_capacity",
            billing_mode="fixed",
            duration_days=30,
            included_gb=0,
            included_users=0,
        )
        sub = SimpleNamespace(
            id=1,
            pg_username="shop1",
            plan_id=None,
            access_status="active",
            started_at=None,
            expires_at=None,
            base_gb=0,
            base_users=0,
            extra_gb_purchased=0,
            extra_users_purchased=0,
            expired_at=None,
            expiry_disabled_user_ids=None,
            warn_sent_at=None,
        )
        session = AsyncMock()
        session.flush = AsyncMock()

        with (
            patch(
                "app.services.pg_admin_subscription.get_or_create_subscription",
                AsyncMock(return_value=sub),
            ),
            patch(
                "app.services.pg_admin_subscription.get_subscription",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.services.pg_admin_subscription.read_live_pg_admin_capacity",
                AsyncMock(return_value=(120, 40)),
            ),
            patch(
                "app.services.pg_admin_subscription._assert_owner_capacity_budget",
                AsyncMock(),
            ) as budget,
            patch(
                "app.services.pg_admin_subscription.sync_pg_capacity_from_sub",
                AsyncMock(),
            ) as sync,
        ):
            out = await start_or_refresh_subscription(
                session,
                pg_username="shop1",
                plan=plan,
                reset_extras=True,
                apply_pg_limits=True,
            )

        self.assertIs(out, sub)
        self.assertEqual(sub.base_gb, 120)
        self.assertEqual(sub.base_users, 40)
        self.assertEqual(sub.plan_id, 7)
        self.assertIsNotNone(sub.expires_at)
        # Recording what the admin already has is not a sale: the owner budget is not checked.
        budget.assert_not_awaited()
        # No extras and preserve mode → do not push (would be no-op / risk clear)
        sync.assert_not_awaited()


class RenewCurrentCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_renew_keeps_base_not_included_zeros(self):
        from app.services.pg_admin_subscription import renew_subscription

        plan = SimpleNamespace(
            id=7,
            plan_kind="current_capacity",
            billing_mode="fixed",
            duration_days=30,
            included_gb=0,
            included_users=0,
            renew_price=0,
            price=0,
            extra_gb_price=0,
            extra_user_price=0,
            renew_pricing_mode="fixed",
            allow_buy_extra=False,
        )
        now = datetime.now(timezone.utc)
        sub = SimpleNamespace(
            id=1,
            pg_username="shop1",
            plan_id=7,
            access_status="active",
            renew_generation=0,
            expires_at=now + timedelta(days=5),
            base_gb=120,
            base_users=40,
            extra_gb_purchased=0,
            extra_users_purchased=0,
            last_renewed_at=None,
            warn_sent_at=None,
            expired_at=None,
            expiry_disabled_user_ids=None,
        )
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=SimpleNamespace(rowcount=1)
        )
        session.refresh = AsyncMock()
        session.commit = AsyncMock()

        with (
            patch(
                "app.services.pg_admin_subscription.restore_subscription_access",
                AsyncMock(return_value={"users_restored": 0, "errors": 0}),
            ),
            patch(
                "app.services.pg_admin_subscription.sync_pg_capacity_from_sub",
                AsyncMock(),
            ) as sync,
            patch("app.services.pasarguard.get_pg", return_value=SimpleNamespace(reset_admin_usage=AsyncMock())),
        ):
            result = await renew_subscription(
                session,
                sub=sub,
                plan=plan,
                payer=None,
                charge_wallet=False,
            )

        self.assertEqual(sub.base_gb, 120)
        self.assertEqual(sub.base_users, 40)
        self.assertEqual(result["amount"], 0)
        sync.assert_awaited()


class ProvisionRejectsCurrentCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_provision_reseller_rejects_current_capacity(self):
        from app.services.resellers import provision_reseller

        plan = SimpleNamespace(
            id=1,
            plan_kind="current_capacity",
            billing_mode="fixed",
            web_permissions=None,
            bot_permissions=None,
            pg_role_id=None,
            create_pg_admin=False,
            share_pg_panel_url=False,
        )
        session = AsyncMock()
        user = SimpleNamespace(id=9, telegram_id=1, role="user")
        with self.assertRaises(ValueError) as ctx:
            await provision_reseller(session, user=user, plan=plan)
        self.assertIn("ظرفیت فعلی", str(ctx.exception))


class SurfaceWiringTests(unittest.TestCase):
    def test_pg_admins_template_defaults_current_capacity(self):
        text = (ROOT / "app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        self.assertIn("current_capacity", text)
        self.assertIn("ظرفیت فعلی", text)
        self.assertIn("default_plan_id", text)

    def test_session_adds_is_system_column(self):
        text = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("is_system", text)

    def test_main_ensures_on_boot(self):
        text = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("ensure_current_capacity_reseller_plan", text)


if __name__ == "__main__":
    unittest.main()
