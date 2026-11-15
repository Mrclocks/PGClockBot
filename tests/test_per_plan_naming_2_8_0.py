"""Per-plan Pasarguard username naming (2.8.0)."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from tests.template_source import template_with_includes


class ResolveUsernameNamingTests(unittest.TestCase):
    def test_empty_plan_inherits_global(self):
        from app.services.orders import resolve_username_naming

        prefix, suffix, pattern = resolve_username_naming(
            {
                "pg_username_prefix": "clk",
                "pg_username_suffix": "x",
                "pg_username_pattern": "{prefix}_{random}{suffix}",
            }
        )
        self.assertEqual(prefix, "clk")
        self.assertEqual(suffix, "x")
        self.assertEqual(pattern, "{prefix}_{random}{suffix}")

    def test_plan_override_wins(self):
        from app.services.orders import resolve_username_naming

        prefix, suffix, pattern = resolve_username_naming(
            {
                "pg_username_prefix": "clk",
                "pg_username_suffix": "x",
                "pg_username_pattern": "{prefix}_{random}",
            },
            plan_prefix="vip",
            plan_suffix="z",
            plan_pattern="{prefix}-{random}-{suffix}",
        )
        self.assertEqual(prefix, "vip")
        self.assertEqual(suffix, "z")
        self.assertEqual(pattern, "{prefix}-{random}-{suffix}")

    def test_blank_plan_fields_inherit(self):
        from app.services.orders import resolve_username_naming

        prefix, suffix, pattern = resolve_username_naming(
            {"pg_username_prefix": "shop", "pg_username_suffix": "", "pg_username_pattern": ""},
            plan_prefix="  ",
            plan_suffix=None,
            plan_pattern="",
        )
        self.assertEqual(prefix, "shop")
        self.assertEqual(suffix, "")
        self.assertEqual(pattern, "{prefix}_{random}{suffix}")


class GenerateWithPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_uses_plan_fields(self):
        from app.services.orders import generate_pg_username

        plan = SimpleNamespace(
            pg_username_prefix="gold",
            pg_username_suffix="s",
            pg_username_pattern="{prefix}_{random}{suffix}",
        )
        with patch(
            "app.services.users.get_all_settings",
            new=AsyncMock(
                return_value={
                    "pg_username_prefix": "clk",
                    "pg_username_suffix": "",
                    "pg_username_pattern": "{prefix}_{random}",
                }
            ),
        ):
            name = await generate_pg_username(AsyncMock(), user_id=7, plan=plan)
        self.assertTrue(name.startswith("gold_"))
        self.assertTrue(name.endswith("s"))


class PlansUiNamingTests(unittest.TestCase):
    def test_custom_modal_has_inline_naming_no_settings_link(self):
        html = template_with_includes("plans.html")
        self.assertNotIn('href="/settings?tab=naming"', html)
        self.assertIn("custom_plan_username_prefix", html)
        self.assertIn("_plan_naming_fields.html", html)
        self.assertIn("trial-naming-sample", html)
        self.assertIn("create-naming-sample", html)

    def test_plan_edit_has_naming(self):
        html = Path("app/web/templates/plan_edit.html").read_text(encoding="utf-8")
        self.assertIn("_plan_naming_fields.html", html)

    def test_model_has_columns(self):
        src = Path("app/db/models.py").read_text(encoding="utf-8")
        self.assertIn("pg_username_prefix", src)
        self.assertIn("pg_username_suffix", src)
        self.assertIn("pg_username_pattern", src)
        migrate = Path("app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("pg_username_prefix", migrate)

    def test_deliver_passes_plan(self):
        src = Path("app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("generate_pg_username(", src)
        # ``{id}`` placeholder uses order id (matches UI / message_variables catalog)
        self.assertIn("user_id=int(order.id)", src)
        # ``{username}`` must resolve via buyer PK, never via order.id
        self.assertIn("buyer_id=int(order.user_id)", src)
        self.assertIn("plan=plan", src)
        self.assertIn("reseller_id=order.reseller_id", src)
        self.assertIn("custom_plan_username_prefix", src)
        self.assertIn("fallback_duration_days=plan_days", src)


if __name__ == "__main__":
    unittest.main()
