"""Reply-keyboard redesign 3.6.0 — submenus with back+home, pay on reply KB."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.bot.keyboards import (
    BTN_BACK,
    DEFAULT_MENU_ORDER,
    REPLY_ACTION_BACK,
    REPLY_ACTION_HOME,
    REPLY_ACTION_PAY_CARD,
    REPLY_ACTION_TOPUP_CARD,
    admin_reply_keyboard,
    cancel_reply,
    main_reply_keyboard,
    pay_reply_keyboard,
    reply_action_map,
    support_reply_keyboard,
    topup_pay_reply_keyboard,
    wallet_reply_keyboard,
)


class ReplyKeyboard360Tests(unittest.TestCase):
    def test_reply_kb_not_force_persistent(self):
        """Non-persistent KB: Android back can hide it; 4-square icon reopens."""
        ui = {
            "menu_layout": "compact",
            "menu_order": "shop,wallet",
            "btn_shop": "خرید",
            "btn_wallet": "کیف",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        for kb in (
            main_reply_keyboard("user", ui=ui),
            wallet_reply_keyboard(ui),
            support_reply_keyboard(ui),
            admin_reply_keyboard(ui),
            cancel_reply(ui),
        ):
            self.assertFalse(kb.is_persistent)

    def test_submenu_has_back_and_home(self):
        ui = {
            "menu_layout": "compact",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        for builder in (wallet_reply_keyboard, support_reply_keyboard, admin_reply_keyboard):
            flat = [b.text for row in builder(ui).keyboard for b in row]
            self.assertIn("⬅️ بازگشت", flat)
            self.assertIn("🏠 منوی اصلی", flat)
            # last row is back + home
            last = builder(ui).keyboard[-1]
            self.assertEqual([b.text for b in last], ["⬅️ بازگشت", "🏠 منوی اصلی"])

    def test_main_omits_home_and_back(self):
        # Option B: level-0 never gets Home/Back footer (nav_mode ignored).
        ui = {
            "nav_mode": "classic",
            "menu_layout": "compact",
            "menu_order": "shop,wallet",
            "btn_shop": "خرید",
            "btn_wallet": "کیف",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        flat = [b.text for row in main_reply_keyboard("user", ui=ui).keyboard for b in row]
        self.assertNotIn("🏠 منوی اصلی", flat)
        self.assertNotIn("⬅️ بازگشت", flat)
        self.assertIn("خرید", flat)

    def test_main_inline_nav_omits_home_footer(self):
        ui = {
            "nav_mode": "inline",
            "menu_layout": "compact",
            "menu_order": "shop,wallet",
            "btn_shop": "خرید",
            "btn_wallet": "کیف",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        flat = [b.text for row in main_reply_keyboard("user", ui=ui).keyboard for b in row]
        self.assertNotIn("🏠 منوی اصلی", flat)
        self.assertNotIn("⬅️ بازگشت", flat)
        self.assertIn("خرید", flat)

    def test_action_map_back_and_home(self):
        ui = {"btn_menu_home": "🏠 منوی اصلی", "btn_back": "⬅️ بازگشت", "menu_order": "shop"}
        mapping = reply_action_map("user", ui=ui)
        self.assertEqual(mapping["🏠 منوی اصلی"], REPLY_ACTION_HOME)
        self.assertEqual(mapping["⬅️ بازگشت"], REPLY_ACTION_BACK)
        self.assertEqual(mapping[BTN_BACK], REPLY_ACTION_BACK)

    def test_pay_and_topup_reply_keyboards(self):
        ui = {
            "menu_layout": "compact",
            "pay_card_enabled": "1",
            "pay_wallet_enabled": "1",
            "btn_pay_card": "🔵💳 کارت",
            "btn_pay_wallet": "👛 کیف",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        pay = pay_reply_keyboard(42, ui)
        flat = [b.text for row in pay.keyboard for b in row]
        self.assertIn("🔵💳 کارت", flat)
        self.assertIn("👛 کیف", flat)
        self.assertIn("⬅️ بازگشت", flat)

        top = topup_pay_reply_keyboard(ui)
        tflat = [b.text for row in top.keyboard for b in row]
        self.assertIn("🔵💳 کارت", tflat)
        self.assertNotIn("👛 کیف", tflat)  # no wallet method on topup

    def test_admin_keyboard_has_hub_groups(self):
        ui = {
            "menu_layout": "compact",
            "btn_adm_orders": "🛒 سفارش‌ها",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        flat = [b.text for row in admin_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("🗓 عملیات روزانه", flat)
        self.assertIn("👤 افراد", flat)
        self.assertIn("📦 محصول و PG", flat)
        self.assertIn("🛠 سیستم", flat)
        # Leaves live in group submenus, not the hub
        self.assertNotIn("📊 داشبورد", flat)
        self.assertNotIn("🤝 نمایندگان", flat)
        self.assertNotIn("💾 بکاپ / ریستور", flat)

        from app.bot.keyboards import (
            admin_ops_reply_keyboard,
            admin_people_reply_keyboard,
            admin_system_reply_keyboard,
        )

        ops = [b.text for row in admin_ops_reply_keyboard(ui).keyboard for b in row]
        people = [b.text for row in admin_people_reply_keyboard(ui).keyboard for b in row]
        system = [b.text for row in admin_system_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("📊 داشبورد", ops)
        self.assertIn("🛒 سفارش‌ها", ops)
        self.assertIn("🤝 نمایندگان", people)
        self.assertIn("💾 بکاپ / ریستور", system)

    def test_guide_still_removed(self):
        self.assertNotIn("guide", DEFAULT_MENU_ORDER)
        self.assertNotIn("faq", DEFAULT_MENU_ORDER)

    def test_chat_menu_helper_importable(self):
        from app.bot.chat_menu import clear_telegram_menu_button, sync_telegram_menu_button

        self.assertTrue(callable(clear_telegram_menu_button))
        self.assertTrue(callable(sync_telegram_menu_button))
        src = Path("app/bot/chat_menu.py").read_text(encoding="utf-8")
        self.assertIn("MenuButtonWebApp", src)
        self.assertIn("miniapp_enabled", src)
        main = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("sync_telegram_menu_button", main)

    def test_pg_reply_keyboard_has_ops_and_nav(self):
        from app.bot.keyboards import pg_reply_keyboard, REPLY_ACTION_PG_USERS, reply_action_map

        ui = {
            "menu_layout": "compact",
            "btn_back": "⬅️ بازگشت",
            "btn_menu_home": "🏠 منوی اصلی",
        }
        flat = [b.text for row in pg_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("👥 کاربران", flat)
        self.assertNotIn("VPN", "".join(flat))
        self.assertIn("🏠 نمای کلی", flat)
        self.assertEqual(pg_reply_keyboard(ui).keyboard[-1][0].text, "⬅️ بازگشت")
        mapping = reply_action_map("admin", ui=ui)
        # Flat map prefers admin hub «کاربران»; PG submenu wins at NAV_ADMIN_PG (reply_nav).
        self.assertEqual(mapping["👥 کاربران"], "adm_users")
        self.assertEqual(mapping["🗓 عملیات روزانه"], "adm_hub_ops")
        self.assertEqual(mapping["👤 افراد"], "adm_hub_people")
        self.assertEqual(mapping["📦 محصول و PG"], "adm_hub_product")
        self.assertEqual(mapping["🛠 سیستم"], "adm_hub_system")
        from pathlib import Path

        nav_src = Path("app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("NAV_ADMIN_PG", nav_src)
        self.assertIn("NAV_ADMIN_OPS", nav_src)
        self.assertIn("_pg_submenu_entries", nav_src)
        self.assertEqual(REPLY_ACTION_PG_USERS, "pg_users")


if __name__ == "__main__":
    unittest.main()
