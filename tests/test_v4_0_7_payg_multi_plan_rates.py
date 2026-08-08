"""v4.0.7 — multiple PAYG reseller plans with per-plan price_per_gb."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]


class PaygPlanModelTests(unittest.TestCase):
    def test_reseller_plan_has_price_per_gb_and_groups(self):
        src = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
        block = src[src.find("class ResellerPlan") : src.find("class ResellerApplicationStatus")]
        self.assertIn("price_per_gb", block)
        self.assertIn("pg_group_ids", block)

    def test_alembic_0004(self):
        path = ROOT / "alembic/versions/0004_reseller_plan_payg_rates.py"
        self.assertTrue(path.is_file())
        src = path.read_text(encoding="utf-8")
        self.assertIn("price_per_gb", src)
        self.assertIn("pg_group_ids", src)
        self.assertIn("0003_reseller_plan_billing_mode", src)

    def test_session_additive_columns(self):
        src = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("reseller_plans ADD COLUMN price_per_gb", src)
        self.assertIn("reseller_plans ADD COLUMN pg_group_ids", src)


class PaygPlanUiTests(unittest.TestCase):
    def test_create_modal_has_payg_rate_and_groups(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("reseller-price-per-gb-field", html)
        self.assertIn('name="price_per_gb"', html)
        self.assertIn('id="reseller-groups"', html)
        self.assertIn("نرخ هر گیگ", html)

    def test_edit_has_payg_rate(self):
        html = (ROOT / "app/web/templates/reseller_plan_edit.html").read_text(encoding="utf-8")
        self.assertIn('name="price_per_gb"', html)
        self.assertIn('id="edit-groups"', html)
        self.assertIn("switch_chip", html)

    def test_crud_wires_price_per_gb(self):
        src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("price_per_gb", src)
        self.assertIn("sync_plan_billing_rate", src)
        self.assertIn("delete_plan_billing_rate", src)
        self.assertIn("_parse_pg_group_ids", src)


class PaygRateResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_falls_back_to_plan_price_per_gb(self):
        from app.services.billing import RateContext, resolve_price_per_gb

        plan = MagicMock()
        plan.price_per_gb = 2500

        session = AsyncMock()
        # rates query → empty
        rates_result = MagicMock()
        rates_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=rates_result)
        session.get = AsyncMock(return_value=plan)

        price = await resolve_price_per_gb(session, RateContext(plan_id=7, reseller_user_id=1))
        self.assertEqual(price, 2500)
        session.get.assert_awaited()

    async def test_rate_context_for_profile_includes_plan_id(self):
        from app.services.billing import rate_context_for_profile

        profile = MagicMock(user_id=9, plan_id=42)
        ctx = rate_context_for_profile(profile)
        self.assertEqual(ctx.reseller_user_id, 9)
        self.assertEqual(ctx.plan_id, 42)

    def test_tick_uses_rate_context_for_profile(self):
        src = (ROOT / "app/services/billing.py").read_text(encoding="utf-8")
        fn = src[src.find("async def tick_reseller_usage") : src.find("async def maybe_warn_low_balance")]
        self.assertIn("rate_context_for_profile", fn)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")


if __name__ == "__main__":
    unittest.main()
