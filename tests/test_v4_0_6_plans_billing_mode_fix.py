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

    def test_init_db_always_runs_additive_ensure(self):
        src = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        # After alembic upgrade, SQLite additive migrator still runs (idempotent).
        # On PostgreSQL it must NOT run — Alembic owns the schema (v11.0.10).
        self.assertIn("await conn.run_sync(_migrate_sqlite_legacy)", src)
        self.assertIn("if _engine_info.is_sqlite:", src)
        upgrade_idx = src.find("upgrade_head(_db_url)")
        ensure_idx = src.find(
            "await conn.run_sync(_migrate_sqlite_legacy)",
            upgrade_idx,
        )
        self.assertGreater(ensure_idx, upgrade_idx)
        # Guard: PG path skips SQLite-era DDL after upgrade.
        pg_guard = src.find("SQLite-era additive migrator", upgrade_idx)
        self.assertGreater(pg_guard, upgrade_idx)

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
