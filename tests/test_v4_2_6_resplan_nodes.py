"""v4.2.6 — reseller plan groups/role required + bot PG nodes parity."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.template_source import template_with_includes

ROOT = Path(__file__).resolve().parents[1]
ADMIN = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
ADMIN_NODES = (ROOT / "app/bot/handlers/admin_pg_nodes.py").read_text(encoding="utf-8")
RESELLER_PAGES = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
PLANS_HTML = template_with_includes("plans.html")
EDIT_HTML = (ROOT / "app/web/templates/reseller_plan_edit.html").read_text(encoding="utf-8")
BOT_INIT = (ROOT / "app/bot/__init__.py").read_text(encoding="utf-8")
RESELLERS = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")


class ResellerPlanRequiredFieldsTests(unittest.TestCase):
    def test_bot_create_requires_groups_then_role(self):
        self.assertIn("AdminPlansStates.res_plan_role", ADMIN_PLANS)
        self.assertIn("حداقل یک گروه الزامی است", ADMIN_PLANS)
        self.assertIn("adm:resplan:add:setrole:", ADMIN_PLANS)
        self.assertIn("نقش پاسارگارد الزامی است", ADMIN_PLANS)
        self.assertNotIn("adm:resplan:add:grpskip", ADMIN_PLANS)
        self.assertIn('raise ValueError("pg_group_ids required")', ADMIN_PLANS)
        self.assertIn('raise ValueError("pg_role_id required")', ADMIN_PLANS)

    def test_bot_fixed_plan_also_asks_groups(self):
        # After price, fixed plans go to group picker (skip commission)
        block = ADMIN_PLANS[
            ADMIN_PLANS.find("async def resplan_price") : ADMIN_PLANS.find(
                "async def resplan_addon_amount"
            )
        ]
        self.assertIn("AdminPlansStates.res_plan_link", block)
        self.assertIn("_show_resplan_add_groups", block)
        self.assertNotIn("res_plan_commission", block)
        self.assertNotIn("_save_reseller_plan", block)

    def test_bot_edit_role_and_groups(self):
        self.assertIn("adm:resplan:edit:role:", ADMIN_PLANS)
        self.assertIn("adm:resplan:edit:setrole:", ADMIN_PLANS)
        self.assertIn("حداقل یک گروه الزامی است", ADMIN_PLANS)

    def test_web_create_requires_groups_and_role(self):
        self.assertIn("reseller-groups", PLANS_HTML)
        # Role required for subscription kinds via JS (addons strip required)
        self.assertIn("reseller-pg-role", PLANS_HTML)
        self.assertIn("setAttribute('required'", PLANS_HTML)
        self.assertIn("حداقل یک گروه پاسارگارد الزامی است", RESELLER_PAGES)
        self.assertIn("نقش پاسارگارد الزامی است", RESELLER_PAGES)

    def test_web_edit_requires_groups_and_role(self):
        self.assertIn("edit-groups", EDIT_HTML)
        self.assertIn("edit-pg-role", EDIT_HTML)
        self.assertIn("setAttribute('required'", EDIT_HTML)
        # Edit save validates both for subscription plans
        edit_save = RESELLER_PAGES[
            RESELLER_PAGES.find("async def reseller_plan_edit_save") : RESELLER_PAGES.find(
                "async def reseller_plan_toggle"
            )
        ]
        self.assertIn("حداقل یک گروه پاسارگارد الزامی است", edit_save)
        self.assertIn("نقش پاسارگارد الزامی است", edit_save)
        # Addon branch may clear groups; subscription path must still require them
        self.assertIn("_is_addon_plan_kind", edit_save)

    def test_detail_shows_groups_and_role(self):
        block = RESELLERS[
            RESELLERS.find("def format_reseller_plan_apply_detail") : RESELLERS.find(
                "async def list_reseller_plans"
            )
        ]
        self.assertIn("گروه‌های پاسارگارد", block)
        self.assertIn("نقش پاسارگارد", block)


class BotPgNodesParityTests(unittest.TestCase):
    def test_router_registered(self):
        self.assertIn("admin_pg_nodes", BOT_INIT)
        self.assertIn("admin_pg_nodes.router", BOT_INIT)

    def test_nodes_ops_callbacks(self):
        for needle in (
            "adm:pg:nodes",
            "adm:pg:nreconall",
            "adm:pg:ncreate",
            "adm:pg:nsync:",
            "adm:pg:nreset:",
            "adm:pg:ntog:",
            "adm:pg:ndel:",
            "adm:pg:recon:",
            "reconnect_all_nodes",
            "sync_node",
            "reset_node",
            "modify_node",
            "delete_node",
            "create_node",
        ):
            self.assertIn(needle, ADMIN_NODES)

    def test_admin_no_longer_owns_node_handlers(self):
        self.assertNotIn('@router.callback_query(F.data == "adm:pg:nodes")', ADMIN)
        self.assertNotIn("adm:pg:recon:", ADMIN)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__
        from app.services.updates import is_same_or_newer

        self.assertTrue(is_same_or_newer(__version__, "0.1.0"))
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__)


if __name__ == "__main__":
    unittest.main()
