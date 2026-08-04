"""3.6.7 — reseller hub on shop /start; ticket manage; shop settings completeness."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Version367Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertGreaterEqual(tuple(int(x) for x in __version__.split(".")), (3, 6, 7))
        self.assertEqual(
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__
        )
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn(f'"{__version__}"', notes)


class ResellerHubStartTests(unittest.TestCase):
    def test_render_home_shop_owner_hub(self):
        src = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        chunk = src.split("async def render_home")[1].split("async def ")[0]
        self.assertIn('effective_role == "reseller" and is_reseller_bot', chunk)
        self.assertIn("reseller_hub_main_keyboard", chunk)
        self.assertIn("پیش‌نمایش منوی کاربر", chunk)

    def test_hub_keyboard_has_preview(self):
        src = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("REPLY_ACTION_RES_PREVIEW", src)
        self.assertIn("def reseller_hub_main_keyboard", src)
        chunk = src.split("def _reseller_submenu_entries")[1].split("\ndef ")[0]
        self.assertIn("REPLY_ACTION_RES_PREVIEW", chunk)

    def test_preview_forces_user_role(self):
        src = (ROOT / "app/bot/menu_nav.py").read_text(encoding="utf-8")
        self.assertIn("elif level == NAV_USER_PREVIEW:", src)
        chunk = src.split("elif level == NAV_USER_PREVIEW:")[1].split("else:")[0]
        self.assertIn("USER.value", chunk)
        self.assertIn("as_user=True", chunk)

    def test_build_main_uses_hub_for_shop_staff(self):
        src = (ROOT / "app/bot/menu_nav.py").read_text(encoding="utf-8")
        chunk = src.split("async def build_main_reply_keyboard")[1].split("\nasync def ")[0]
        self.assertIn('role == "reseller" and is_reseller_bot and not as_user', chunk)
        self.assertIn("reseller_hub_main_keyboard", chunk)


class TicketManageTests(unittest.TestCase):
    def test_res_tickets_has_action_buttons(self):
        src = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        chunk = src.split("async def res_tickets")[1].split("@router.")[0]
        self.assertIn("tkt:reply:", chunk)
        self.assertIn("tkt:close:", chunk)
        self.assertIn("InlineKeyboardMarkup", chunk)

    def test_ticket_count_uses_ticket_reseller_id(self):
        src = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn("Ticket.reseller_id == owner_id", src)

    def test_with_shop_settings_injects_tickets(self):
        from app.services.resellers import with_shop_settings

        perms = with_shop_settings([])
        self.assertIn("tickets", perms)
        self.assertIn("shop_settings", perms)
        self.assertIn("dashboard", perms)


class ShopSettingsCompletenessTests(unittest.TestCase):
    def test_naming_tab_for_reseller(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS

        keys = {k for k, _ in RESELLER_SETTINGS_TABS}
        self.assertIn("menu", keys)
        self.assertIn("naming", keys)
        self.assertIn("buttons", keys)

    def test_telegram_menu_order(self):
        src = (ROOT / "app/bot/handlers/reseller_settings.py").read_text(encoding="utf-8")
        self.assertIn('"menu_order"', src)
        self.assertIn("res:st:menu:up:", src)
        self.assertIn("res:st:menu:add:", src)
        self.assertIn("res:st:menu:rm:", src)

    def test_web_menu_copy(self):
        src = (ROOT / "app/web/templates/shop_settings.html").read_text(encoding="utf-8")
        self.assertIn("جفتی/تکی", src)
        self.assertIn("افزودن/حذف", src)

    def test_web_permissions_empty_string_lockdown(self):
        """Empty web_permissions must not soft-upgrade on web (align with has_perm)."""
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_FEATURE_PERMS", src)
        self.assertIn("with_shop_settings(parsed) if parsed else parsed", src)

    def test_shop_buttons_filter_platform_labels(self):
        src = (ROOT / "app/api/shop_settings.py").read_text(encoding="utf-8")
        self.assertIn("btn_admin", src)
        self.assertIn("_shop_btn_block", src)


if __name__ == "__main__":
    unittest.main()
