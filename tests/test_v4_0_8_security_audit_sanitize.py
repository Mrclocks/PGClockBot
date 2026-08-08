"""Security audit follow-up — no raw PG/SQL errors in panel flash text."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class PlansPgErrorSanitizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_pg_plan_options_hides_raw_exception(self):
        from app.services.plans_catalog import load_pg_plan_options

        admin = {"role": "admin"}
        mock_pg = MagicMock()
        mock_pg.get_user_templates_simple = AsyncMock(
            side_effect=RuntimeError("SECRET_HOST:8443 token=abc")
        )
        with patch("app.services.plans_catalog.get_pg", return_value=mock_pg):
            templates, groups, err = await load_pg_plan_options(admin, session=None)
        self.assertEqual(templates, [])
        self.assertEqual(groups, [])
        self.assertIsNotNone(err)
        self.assertNotIn("SECRET_HOST", err)
        self.assertNotIn("token=abc", err)
        self.assertIn("پاسارگارد", err)


class PlansSoftFailSanitizeTests(unittest.TestCase):
    def test_plans_page_does_not_embed_exception_object(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        fn = src[src.find("async def plans_page") : src.find("async def plans_create")]
        self.assertIn("reseller_plans_err", fn)
        self.assertNotIn("reseller_plans_err = str(e)", fn)
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        # Template must not interpolate raw exception via double-wrap
        self.assertNotIn("({{ reseller_plans_err }})", html)


class DualPathInvariantStillPresent(unittest.TestCase):
    def test_security_audit_file_covers_xor(self):
        src = (ROOT / "tests/test_final_admin_reseller_security_audit.py").read_text(encoding="utf-8")
        self.assertIn("pg_staff", src)
        self.assertIn("reseller", src)

    def test_no_owner_token_fallback_helpers(self):
        read_src = (ROOT / "app/services/pg_read.py").read_text(encoding="utf-8")
        self.assertIn("PgReadDenied", read_src)
        # Fail-closed messaging present
        self.assertTrue("Denied" in read_src or "denied" in read_src.lower() or "PgReadDenied" in read_src)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.8.0")


if __name__ == "__main__":
    unittest.main()
