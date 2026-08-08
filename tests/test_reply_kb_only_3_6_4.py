"""3.6.4 — reply-only static menus; fix reseller/admin hubs; entity names stay inline."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class Version364Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.6.8"', notes)


class AdminResellersHubTests(unittest.TestCase):
    def test_adm_resellers_restores_reply_kb(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        chunk = src.split("async def adm_resellers")[1].split("RESELLERS_PAGE_SIZE")[0]
        self.assertIn("admin_resellers_reply_keyboard()", chunk)
        self.assertIn("NAV_ADMIN_RESELLERS", chunk)

    def test_list_keyboard_no_back_chrome(self):
        from app.bot.keyboards import admin_resellers_list_keyboard, admin_users_list_keyboard
        from aiogram.types import InlineKeyboardButton

        rows = [[InlineKeyboardButton(text="🤝 Ali", callback_data="adm:users:view:1")]]
        kb = admin_resellers_list_keyboard(page=0, has_prev=False, has_next=False, rows=rows)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Ali" in t for t in texts))
        self.assertFalse(any("بازگشت" in t for t in texts))

        ukb = admin_users_list_keyboard(page=0, has_prev=False, has_next=False, rows=rows)
        utexts = [b.text for row in ukb.inline_keyboard for b in row]
        self.assertFalse(any("جستجو" in t for t in utexts))
        self.assertFalse(any("بازگشت" in t for t in utexts))


class ResellerNavFixTests(unittest.TestCase):
    def test_preserve_state_includes_res_hubs(self):
        src = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn('"res_settings"', src)
        self.assertIn('"res_plans"', src)
        self.assertIn("NAV_RESELLER_PLANS", src)

    def test_submenu_entries_fail_closed(self):
        from app.bot.keyboards import _reseller_submenu_entries, reply_action_map

        self.assertEqual(_reseller_submenu_entries(None), [])
        mapping = reply_action_map(
            "reseller",
            include_submenus=True,
            is_reseller_bot=True,
            profile=None,
            ui={"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"},
        )
        self.assertNotIn("💎 پلن‌های فروش", mapping)
        self.assertNotIn("⚙️ تنظیمات فروشگاه", mapping)

    def test_full_profile_maps_all(self):
        from app.bot.keyboards import reply_action_map

        profile = SimpleNamespace(
            is_active=True,
            web_permissions="dashboard,plans,orders,payments,tickets,stats,shop_settings",
        )
        mapping = reply_action_map(
            "reseller",
            include_submenus=True,
            is_reseller_bot=True,
            profile=profile,
            ui={"btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"},
        )
        self.assertEqual(mapping["💎 پلن‌های فروش"], "res_plans")
        self.assertEqual(mapping["⚙️ تنظیمات فروشگاه"], "res_settings")
        self.assertEqual(mapping["➕ پلن جدید"], "res_plan_add")


class EntityOnlyInlineTests(unittest.TestCase):
    def test_services_keyboard_names_only(self):
        from app.bot.keyboards import services_keyboard

        class S:
            id = 1
            pg_username = "clk_demo"

        kb = services_keyboard([S()])
        texts = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("clk_demo" in t for t in texts))
        self.assertFalse(any("بازگشت" in t for t in texts))

    def test_service_actions_reply_exists(self):
        from app.bot.keyboards import service_actions, service_actions_reply_keyboard

        self.assertEqual(service_actions(1).inline_keyboard, [])
        flat = [b.text for row in service_actions_reply_keyboard().keyboard for b in row]
        self.assertIn("♻️ رفرش وضعیت", flat)
        self.assertIn("⬅️ بازگشت", flat)

    def test_plan_item_no_list_back(self):
        src = (ROOT / "app/bot/handlers/reseller_plans.py").read_text(encoding="utf-8")
        chunk = src.split("def _plan_item_kb")[1].split("async def _list_plans")[0]
        self.assertNotIn("res:plans", chunk)


if __name__ == "__main__":
    unittest.main()
