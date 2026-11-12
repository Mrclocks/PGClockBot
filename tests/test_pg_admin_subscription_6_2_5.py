"""PG admin subscription: renew invoice, plan kinds, expiry gating."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class RenewInvoiceTests(unittest.TestCase):
    def test_fixed_mode_ignores_extras(self):
        from app.services.pg_admin_subscription import compute_renew_invoice

        plan = SimpleNamespace(
            renew_price=100_000,
            price=80_000,
            extra_gb_price=1000,
            extra_user_price=2000,
            renew_pricing_mode="fixed",
            allow_buy_extra=False,
            duration_days=30,
            included_gb=30,
            included_users=30,
        )
        sub = SimpleNamespace(
            extra_gb_purchased=50,
            extra_users_purchased=20,
            base_gb=30,
            base_users=30,
            expires_at=None,
            access_status="active",
        )
        inv = compute_renew_invoice(plan, sub)
        self.assertEqual(inv["mode"], "fixed")
        self.assertEqual(inv["total"], 100_000)
        self.assertEqual(inv["extras_gb_amount"], 0)

    def test_buy_extra_forces_from_capacity_even_if_stored_fixed(self):
        from app.services.pg_admin_subscription import (
            compute_renew_invoice,
            resolve_renew_pricing_mode,
        )

        plan = SimpleNamespace(
            renew_price=0,
            price=10,
            extra_gb_price=1,
            extra_user_price=1,
            renew_pricing_mode="fixed",  # misconfigured / legacy
            allow_buy_extra=True,
            duration_days=30,
        )
        sub = SimpleNamespace(
            extra_gb_purchased=30,
            extra_users_purchased=10,
            base_gb=10,
            base_users=10,
            expires_at=None,
            access_status="active",
        )
        self.assertEqual(resolve_renew_pricing_mode(plan), "from_capacity")
        inv = compute_renew_invoice(plan, sub)
        self.assertEqual(inv["mode"], "from_capacity")
        self.assertEqual(inv["total"], 10 + 30 + 10)  # 50

    def test_from_capacity_adds_extras(self):
        from app.services.pg_admin_subscription import compute_renew_invoice

        plan = SimpleNamespace(
            renew_price=100_000,
            price=80_000,
            extra_gb_price=1_000,
            extra_user_price=2_000,
            renew_pricing_mode="from_capacity",
            duration_days=30,
            included_gb=30,
            included_users=30,
        )
        sub = SimpleNamespace(
            extra_gb_purchased=10,
            extra_users_purchased=5,
            base_gb=30,
            base_users=30,
            expires_at=None,
            access_status="active",
        )
        inv = compute_renew_invoice(plan, sub)
        self.assertEqual(inv["mode"], "from_capacity")
        self.assertEqual(inv["total"], 100_000 + 10 * 1_000 + 5 * 2_000)
        self.assertEqual(inv["total_gb"], 40)
        self.assertEqual(inv["total_users"], 35)


class PlanKindTests(unittest.TestCase):
    def test_kind_helpers(self):
        from app.services.pg_admin_subscription import (
            is_addon_plan,
            is_subscription_plan,
            plan_kind_of,
        )

        self.assertEqual(plan_kind_of(None), "subscription")
        self.assertTrue(is_subscription_plan(SimpleNamespace(plan_kind="subscription")))
        self.assertTrue(is_subscription_plan(SimpleNamespace(plan_kind="current_capacity")))
        self.assertTrue(is_addon_plan(SimpleNamespace(plan_kind="addon_volume")))
        self.assertTrue(is_addon_plan(SimpleNamespace(plan_kind="addon_users")))
        self.assertFalse(is_subscription_plan(SimpleNamespace(plan_kind="addon_volume")))


class ModelAndMigrationSurfaceTests(unittest.TestCase):
    def test_model_has_subscription_table(self):
        from app.db.models import PgAdminSubscription, ResellerPlan

        self.assertTrue(hasattr(ResellerPlan, "plan_kind"))
        self.assertTrue(hasattr(ResellerPlan, "duration_days"))
        self.assertTrue(hasattr(PgAdminSubscription, "pg_username"))
        self.assertTrue(hasattr(PgAdminSubscription, "extra_gb_purchased"))

    def test_alembic_revision_exists(self):
        path = ROOT / "alembic/versions/0011_pg_admin_subscription.py"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("pg_admin_subscriptions", text)
        self.assertIn("plan_kind", text)

    def test_scheduler_registers_tick(self):
        src = (ROOT / "app/jobs/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("run_pg_admin_subscription_tick", src)
        self.assertIn("pg_admin_subscription", src)

    def test_renew_uses_subscription_service(self):
        from app.services import pg_admin_subscription, reseller_package

        self.assertIs(reseller_package.renew_subscription, pg_admin_subscription.renew_subscription)


class EarlyRenewAnchorTests(unittest.TestCase):
    def test_invoice_html_includes_total(self):
        from app.services.pg_admin_subscription import format_renew_invoice_html

        html = format_renew_invoice_html(
            {
                "mode": "from_capacity",
                "base": 1000,
                "extra_gb": 2,
                "extra_users": 1,
                "extras_gb_amount": 200,
                "extras_users_amount": 100,
                "total": 1300,
                "total_gb": 32,
                "total_users": 31,
                "duration_days": 30,
            },
            currency="تومان",
        )
        self.assertIn("1300", html.replace(",", "").replace("٬", ""))
        self.assertIn("حجم اضافه", html)


if __name__ == "__main__":
    unittest.main()
