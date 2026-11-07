"""Redesigned plan modal: same form fields as before, 14 suggested permissions, grouped switches."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import templates
from app.services.button_styles import ITEM_BUTTON_STYLE_OPTIONS, PLAN_BUTTON_STYLE_OPTIONS
from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS, feature_perm_groups

ROOT = Path(__file__).resolve().parents[1]

# Every field the reseller plan create route read from the form before the redesign.
RESELLER_FORM_FIELDS = {
    "billing_mode", "plan_kind", "category_id", "name", "price", "price_per_gb", "sort_order", "description",
    "duration_days", "included_gb", "included_users", "addon_gb", "addon_users", "renew_price",
    "renew_pricing_mode", "create_pg_admin", "share_pg_panel_url", "carry_remaining_period",
    "keep_extras_on_renew", "allow_buy_extra", "extra_gb_price", "extra_user_price", "extra_gb_amount",
    "extra_gb_style", "extra_user_amount", "extra_user_style", "pg_role_id", "is_active", "button_style",
}
RISKY = {"payment_void", "wallet_adjust", "loyalty_adjust", "orders_purge", "free_checkout"}


def render_reseller_form() -> str:
    context = {
        "groups": [{"id": 3, "name": "Germany"}, {"id": 4, "name": "<b>Fin</b>"}],
        "pg_roles": [{"id": 1, "name": "reseller"}],
        "plan_categories": [],
        "pg_limit_cards": [],
        "plan_style_options": PLAN_BUTTON_STYLE_OPTIONS,
        "pack_style_options": ITEM_BUTTON_STYLE_OPTIONS,
        "request": SimpleNamespace(query_params={}),
    }
    return templates.env.get_template("_reseller_plan_form.html").render(**context)


def field_names(html: str) -> set[str]:
    return set(re.findall(r'\bname="([a-z_0-9]+)"', html))


def checked_perms(html: str) -> set[str]:
    return set(re.findall(r'name="perm_([a-z_]+)" value="1"[^>]*\bchecked\b', html))


class FeaturePermGroupTests(unittest.TestCase):
    def test_every_permission_is_in_exactly_one_group(self) -> None:
        keys = [p.key for g in feature_perm_groups() for p in g.perms]
        self.assertEqual(sorted(keys), sorted(k for k, _ in FEATURE_PERMS))
        self.assertEqual(len(keys), len(set(keys)))

    def test_suggested_defaults_match_the_server_default(self) -> None:
        on = {p.key for g in feature_perm_groups() for p in g.perms if p.default}
        self.assertEqual(on, set(DEFAULT_FEATURE_PERMS.split(",")))
        self.assertEqual(len(on), 14)

    def test_money_and_purge_actions_are_one_risky_group_that_starts_off(self) -> None:
        risky = [g for g in feature_perm_groups() if g.risky]
        self.assertEqual(len(risky), 1)
        self.assertEqual({p.key for p in risky[0].perms}, RISKY)
        self.assertFalse(any(p.default for p in risky[0].perms))


class ResellerPlanFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = render_reseller_form()

    def test_form_posts_every_field_the_route_reads(self) -> None:
        names = field_names(self.html)
        self.assertLessEqual(RESELLER_FORM_FIELDS, names)
        self.assertIn("group_3", names)
        self.assertEqual({n for n in names if n.startswith("perm_")}, {f"perm_{k}" for k, _ in FEATURE_PERMS})

    def test_create_form_starts_with_the_fourteen_suggested_permissions(self) -> None:
        self.assertEqual(checked_perms(self.html), set(DEFAULT_FEATURE_PERMS.split(",")))
        self.assertFalse(checked_perms(self.html) & RISKY)

    def test_reset_restores_the_server_defaults(self) -> None:
        for key, _ in FEATURE_PERMS:
            default = "1" if key in DEFAULT_FEATURE_PERMS.split(",") else "0"
            self.assertRegex(self.html, rf'name="perm_{key}" value="1"[^>]*data-default="{default}"')

    def test_role_is_required_and_groups_need_one_choice(self) -> None:
        self.assertRegex(self.html, r'name="pg_role_id" id="reseller-pg-role" required')
        self.assertIn("data-ds-require-one", self.html)

    def test_group_names_from_the_panel_are_escaped(self) -> None:
        self.assertIn("&lt;b&gt;Fin&lt;/b&gt;", self.html)
        self.assertNotIn("<b>Fin</b>", self.html)

    def test_delete_buttons_are_icons_with_a_label(self) -> None:
        self.assertNotIn(">حذف</button>", self.html)
        self.assertIn('aria-label="حذف بسته حجم"', self.html)


@unittest.skipUnless(subprocess.run(["node", "--version"], capture_output=True).returncode == 0, "node not installed")
class PackPriceScriptTests(unittest.TestCase):
    def test_node_suite(self) -> None:
        result = subprocess.run(["node", str(ROOT / "tests" / "pack_rows.test.cjs")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
