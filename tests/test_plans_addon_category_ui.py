"""Plans UI polish: deleted template warn, category placement, gift expire width, addon category."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.template_source import template_with_includes

ROOT = Path(__file__).resolve().parents[1]


class PlansAddonCategoryUiTests(unittest.TestCase):
    def test_add_plan_category_sits_with_the_bot_display_options(self):
        html = template_with_includes("plans.html")
        fixed = html.split('id="panel-user-fixed"', 1)[1].split(
            'id="panel-user-trial"', 1
        )[0]
        display = fixed.split("نمایش در ربات", 1)[1]
        self.assertIn("برچسب دسته", display)
        self.assertIn('data-category-audience="users"', display)
        self.assertLess(fixed.find("نام پلن"), fixed.find("برچسب دسته"))

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

    def test_bot_and_miniapp_filter_by_service_category(self):
        bot = (ROOT / "app/bot/handlers/services.py").read_text(encoding="utf-8")
        self.assertIn("list_packs_for_service", bot)
        mini = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
        self.assertIn("list_packs_for_service", mini)
        self.assertIn("pack_matches_service", mini)
        res = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn("category_id=sub_cat", res)


if __name__ == "__main__":
    unittest.main()
