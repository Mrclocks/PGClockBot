"""v4.2.1 — bot plans hub: reply audience/kind, correct backs, reseller CRUD."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
KEYBOARDS = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
REPLY_NAV = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
MENU_NAV = (ROOT / "app/bot/menu_nav.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")


class PlansBackLinkTests(unittest.TestCase):
    def test_plans_module_no_settings_back_for_trial_custom(self):
        """Trial/custom in plans hub must not link back to adm:st:sec:service."""
        self.assertNotIn("adm:st:sec:service", ADMIN_PLANS)
        self.assertNotIn("adm:st:sub:service:custom", ADMIN_PLANS)
        self.assertNotIn("adm:st:sub:service:trial", ADMIN_PLANS)

    def test_kind_screens_back_to_audience_not_same_kind(self):
        """«انتخاب نوع» must return to kind hub, not re-open the same screen."""
        self.assertIn("BACK_USERS_KIND = \"adm:plans:aud:users\"", ADMIN_PLANS)
        self.assertIn("_back_row(\"⬅️ پلن‌های کاربران\", BACK_USERS_KIND)", ADMIN_PLANS)
        self.assertNotIn("_back_row(\"⬅️ انتخاب نوع\", BACK_USERS_TRIAL)", ADMIN_PLANS)

    def test_wholesale_in_bot_not_web_only(self):
        self.assertIn("adm:plans:tog:wholesale_enabled", ADMIN_PLANS)
        self.assertIn("wholesale_tiers", ADMIN_PLANS)
        self.assertNotIn("default_panel_base_url", ADMIN_PLANS.split("wholesale")[1][:500])

    def test_reseller_plan_create_and_edit_in_bot(self):
        self.assertIn("adm:resplan:add:", ADMIN_PLANS)
        self.assertIn("ResellerPlan(", ADMIN_PLANS)
        self.assertIn("adm:resplan:view:", ADMIN_PLANS)
        self.assertIn("adm:resplan:edit:name:", ADMIN_PLANS)
        self.assertIn("adm:resplan:perms:", ADMIN_PLANS)
        self.assertNotIn("adm:resplan:hint", ADMIN_PLANS)

    def test_user_plan_delete_in_bot(self):
        self.assertIn("adm:plan:delask:", ADMIN)
        self.assertIn("adm:plan:del:", ADMIN)

    def test_no_duplicate_adm_plans_kind_in_admin(self):
        self.assertNotIn("async def adm_plans_kind", ADMIN)
        self.assertNotIn("adm:resplan:hint", ADMIN)

    def test_reply_keyboard_sync_helper(self):
        self.assertIn("sync_plans_reply_keyboard", ADMIN_PLANS)


class ReplyKeyboardAudienceTests(unittest.TestCase):
    def test_audience_on_reply_keyboard(self):
        self.assertIn("REPLY_ACTION_ADM_PLANS_AUD_USERS", KEYBOARDS)
        self.assertIn("REPLY_ACTION_ADM_PLANS_AUD_RESELLERS", KEYBOARDS)
        self.assertIn("def admin_plans_audience_reply_keyboard", KEYBOARDS)
        self.assertIn("def admin_plans_kind_reply_keyboard", KEYBOARDS)

    def test_reply_nav_handles_audience_and_kind(self):
        self.assertIn("REPLY_ACTION_ADM_PLANS_AUD_USERS", REPLY_NAV)
        self.assertIn("NAV_ADMIN_PLANS_AUDIENCE", REPLY_NAV)
        self.assertIn("NAV_ADMIN_PLANS_KIND", REPLY_NAV)

    def test_menu_nav_levels(self):
        self.assertIn("NAV_ADMIN_PLANS_AUDIENCE", MENU_NAV)
        self.assertIn("NAV_ADMIN_PLANS_KIND", MENU_NAV)


class InlineBackSanityTests(unittest.TestCase):
    def test_reseller_list_uses_view_not_hint(self):
        block = KEYBOARDS[
            KEYBOARDS.find("def admin_reseller_plans_list_keyboard")
            : KEYBOARDS.find("def admin_users_keyboard")
        ]
        self.assertIn("adm:resplan:view:", block)
        self.assertNotIn("adm:resplan:hint", block)
        self.assertIn("adm:plans:aud:resellers", block)

    def test_user_plan_list_back_to_kind_hub(self):
        block = KEYBOARDS[
            KEYBOARDS.find("def admin_plans_list_keyboard")
            : KEYBOARDS.find("def admin_reseller_plans_list_keyboard")
        ]
        self.assertIn("adm:plans:aud:users", block)

    def test_user_plan_detail_has_list_back(self):
        detail = ADMIN[ADMIN.find("def _plan_detail_keyboard") : ADMIN.find("router = Router")]
        self.assertIn("adm:plans:aud:users", detail)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")


if __name__ == "__main__":
    unittest.main()
