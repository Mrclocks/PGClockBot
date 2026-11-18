"""v4.0.6 — plans page 500 fix (billing_mode migrate) + remove reseller Plans tab."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BillingModeMigrateTests(unittest.TestCase):
    def test_alembic_revision_exists(self):
        path = ROOT / "alembic/versions/0003_reseller_plan_billing_mode.py"
        self.assertTrue(path.is_file())
        src = path.read_text(encoding="utf-8")
        self.assertIn("billing_mode", src)
        self.assertIn("0002_pg_staff_credentials", src)
        self.assertIn("reseller_plans", src)

    def test_plans_page_soft_fails_reseller_list(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        fn = src[src.find("async def plans_page") : src.find("async def plans_create")]
        self.assertIn("reseller_plans_err", fn)
        self.assertIn("list_reseller_plans(session)", fn)
        self.assertIn("except Exception", fn)


class ResellerPlansTabRemovedTests(unittest.TestCase):
    def test_tabs_no_longer_include_plans(self):
        src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        fn = src[src.find("def _tabs") : src.find("@app.get(\"/resellers\"")]
        self.assertNotIn("/plans#reseller-plans", fn)
        self.assertNotIn('"پلن‌ها"', fn)
        self.assertIn("/resellers/applications", fn)
        self.assertIn("لیست", fn)

    def test_unified_plans_still_has_reseller_section(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn('id="reseller-plans"', html)
        self.assertIn("پلن‌های نمایندگان", html)
        self.assertIn("reseller_plans_err", html)


if __name__ == "__main__":
    unittest.main()
