"""v4.2.2 — plans reply labels must not collide with admin users/resellers hub."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYBOARDS = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
REPLY_KB = (ROOT / "app/bot/reply_keyboards.py").read_text(encoding="utf-8")
REPLY_NAV = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
ADMIN_PLANS = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")


class PlansLabelCollisionTests(unittest.TestCase):
    def test_audience_labels_distinct_from_admin_users_resellers(self):
        # Labels come from settings now; check the buttons the audience step builds.
        from app.bot.keyboards import _t
        from app.bot.reply_keyboards import _admin_plans_audience_entries
        from app.services.users import DEFAULT_SETTINGS

        ui = dict(DEFAULT_SETTINGS)
        labels = [text for _action, text in _admin_plans_audience_entries(ui)]
        self.assertEqual(
            labels, ["📦 پلن‌های کاربران", "🤝 پلن‌های نمایندگان", "🏷 برچسب دسته", "⏱ بسته حجم و زمان"]
        )
        for hub_label in (_t(ui, "btn_adm_users"), _t(ui, "btn_adm_resellers")):
            self.assertNotIn(hub_label, labels)

    def test_reply_nav_resolves_plans_nav_levels(self):
        self.assertIn("NAV_ADMIN_PLANS_AUDIENCE", REPLY_NAV)
        self.assertIn("_admin_plans_audience_entries(ui)", REPLY_NAV)
        self.assertIn("NAV_ADMIN_PLANS_KIND", REPLY_NAV)
        self.assertIn("_admin_plans_list_entries", REPLY_NAV)
        self.assertIn("_admin_plans_add_type_entries", REPLY_NAV)
        self.assertIn("NAV_ADMIN_PLANS_AUDIENCE", REPLY_NAV.split("Prefer admin hub")[0])

    def test_no_duplicate_audience_hub_on_open(self):
        block = REPLY_NAV[REPLY_NAV.find("async def open_admin_plans_hub") : REPLY_NAV.find("async def open_reseller_settings_hub")]
        self.assertNotIn("send_audience_hub", block)

    def test_users_overview_like_web(self):
        self.assertIn("send_users_plans_overview", ADMIN_PLANS)
        self.assertIn("send_resellers_plans_overview", ADMIN_PLANS)
        self.assertIn("admin_users_plans_overview_keyboard", KEYBOARDS)
        self.assertIn("admin_resellers_plans_overview_keyboard", KEYBOARDS)

    def test_user_fixed_plan_edit_in_bot(self):
        self.assertIn("adm:plan:edit:name:", ADMIN)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__
        from app.services.updates import is_same_or_newer

        self.assertTrue(is_same_or_newer(__version__, "0.1.0"))
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__)


if __name__ == "__main__":
    unittest.main()
