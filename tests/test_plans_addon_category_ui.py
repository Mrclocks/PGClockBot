"""Plans UI polish: deleted template warn, category placement, gift expire width, addon category."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.template_source import template_with_includes

ROOT = Path(__file__).resolve().parents[1]


class PlansAddonCategoryUiTests(unittest.TestCase):
    def test_add_plan_category_sits_right_under_the_plan_name(self):
        # Rendered form: the category is chosen right after the name (the first thing the bot groups plans by),
        # before the price, and only once.
        from app.api.app import templates
        from app.db.models import PlanCategory

        html = templates.env.get_template("_user_plan_forms.html").render(
            plan_categories=[PlanCategory(id=1, name="ویژه", audience="users", is_active=True)],
            plan_category_tones={1: "success"},
            groups=[], templates=[], values={}, plan_style_options=[], pack_style_options=[],
        )
        fixed = html.split('id="panel-user-fixed"', 1)[1].split('id="panel-user-trial"', 1)[0]
        name, category, price = (fixed.find(f'name="{field}"') for field in ("name", "category_id", "price"))
        self.assertTrue(0 <= name < category < price)
        self.assertEqual(fixed.count('name="category_id"'), 1)
        self.assertIn('data-value="1" data-tone="success"', fixed)

    def test_template_missing_warn_wiring(self):
        html = template_with_includes("plans.html")
        self.assertIn("tpl_missing", html)
        self.assertIn("plan-pg-warn-dot", html)
        self.assertIn('badge warn', html)
        self.assertIn("خالی", html)
        app = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("pg_live_template_ids", app)

    def test_reseller_addon_category_not_hidden(self):
        html = template_with_includes("plans.html")
        self.assertIn("setVisible(catField, true)", html)
        self.assertNotIn("setVisible(catField, !isAddon)", html)
        edit = (ROOT / "app/web/templates/reseller_plan_edit.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("edit-reseller-category-field", edit)
        # Category field is no longer gated behind {% if not is_addon %}
        self.assertNotIn("{% if not is_addon %}\n    <label class=\"form-field\" id=\"edit-reseller-category-field\"", edit)

    def test_model_and_migration_have_pack_category(self):
        models = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
        self.assertIn("class ServiceAddonPack", models)
        pack_block = models.split("class ServiceAddonPack", 1)[1].split(
            "class Order", 1
        )[0]
        self.assertIn("category_id", pack_block)
        mig = (
            ROOT / "alembic/versions/0044_service_addon_pack_category.py"
        ).read_text(encoding="utf-8")
        self.assertIn("0043_reseller_plan_is_system", mig)
        self.assertIn("service_addon_packs", mig)


if __name__ == "__main__":
    unittest.main()
