"""v4.2.3 — bot plans dashboard rebuild: inline list + add on reply keyboard."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
KEYBOARDS = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
REPLY_NAV = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
MENU_NAV = (ROOT / "app/bot/menu_nav.py").read_text(encoding="utf-8")


class PlansDashboardRebuildTests(unittest.TestCase):
    def test_add_plan_on_reply_keyboard_not_kind_picker(self):
        self.assertIn("REPLY_ACTION_ADM_PLANS_ADD", KEYBOARDS)
        self.assertIn("➕ افزودن پلن", KEYBOARDS)
        list_block = KEYBOARDS[
            KEYBOARDS.find("def _admin_plans_list_entries")
            : KEYBOARDS.find("def _admin_plans_add_type_entries")
        ]
        self.assertIn("REPLY_ACTION_ADM_PLANS_ADD", list_block)
        self.assertNotIn("REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED", list_block)

    def test_type_picker_after_add(self):
        self.assertIn("def admin_plans_add_type_keyboard", KEYBOARDS)
        self.assertIn("adm:plans:add:users:fixed", KEYBOARDS)
        self.assertIn("adm:plans:add:resellers:payg", KEYBOARDS)
        self.assertIn("send_add_plan_type_picker", ADMIN_PLANS)
        self.assertIn("open_add_kind_action", ADMIN_PLANS)

    def test_overview_lists_plans_inline(self):
        ov_block = KEYBOARDS[
            KEYBOARDS.find("def admin_users_plans_overview_keyboard")
            : KEYBOARDS.find("def admin_resellers_plans_overview_keyboard")
        ]
        self.assertIn("adm:plan:view:", ov_block)
        self.assertIn("adm:plans:kind:users:trial", ov_block)
        self.assertNotIn("adm:plan:add", ov_block)
        res_block = KEYBOARDS[
            KEYBOARDS.find("def admin_resellers_plans_overview_keyboard")
            : KEYBOARDS.find("def admin_plans_add_type_keyboard")
        ]
        self.assertIn("adm:resplan:view:", res_block)
        self.assertNotIn("adm:resplan:add:", res_block)

    def test_reply_nav_wires_add_flow(self):
        self.assertIn("REPLY_ACTION_ADM_PLANS_ADD", REPLY_NAV)
        self.assertIn("send_add_plan_type_picker", REPLY_NAV)
        self.assertIn("NAV_ADMIN_PLANS_ADD_TYPE", REPLY_NAV)

    def test_nav_level_for_add_type(self):
        self.assertIn("NAV_ADMIN_PLANS_ADD_TYPE", MENU_NAV)
        self.assertIn("admin_plans_add_type_reply_keyboard", MENU_NAV)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.8.0")


if __name__ == "__main__":
    unittest.main()
