"""Keyboard menu restore + guide/faq removal (3.5.7)."""

from __future__ import annotations

import unittest

from app.bot.keyboards import (
    DEFAULT_MENU_ORDER,
    REMOVED_MENU_KEYS,
    admin_reply_keyboard,
    cancel_reply,
    is_cancel_text,
    is_home_text,
    main_reply_keyboard,
    reply_action_map,
    support_reply_keyboard,
    wallet_reply_keyboard,
)


class KeyboardMenu357Tests(unittest.TestCase):
    def test_default_order_has_no_guide_faq(self):
        self.assertNotIn("guide", DEFAULT_MENU_ORDER)
        self.assertNotIn("faq", DEFAULT_MENU_ORDER)
        self.assertTrue({"guide", "faq", "restart"} <= REMOVED_MENU_KEYS)

    def test_submenus_with_home_not_force_persistent(self):
        ui = {"btn_menu_home": "🏠 منوی اصلی", "menu_layout": "compact", "btn_back": "⬅️ بازگشت"}
        for kb in (wallet_reply_keyboard(ui), support_reply_keyboard(ui)):
            self.assertFalse(kb.is_persistent)
            flat = [b.text for row in kb.keyboard for b in row]
            self.assertIn("🏠 منوی اصلی", flat)
            self.assertIn("⬅️ بازگشت", flat)

    def test_submenu_labels_in_action_map(self):
        mapping = reply_action_map(
            "user",
            has_services=False,
            ui={"menu_order": "shop,wallet,support", "btn_shop": "خرید", "btn_wallet": "کیف", "btn_support": "پشتیبانی", "btn_menu_home": "🏠 منوی اصلی"},
            include_submenus=True,
        )
        self.assertIn("🟢➕ شارژ کیف پول", mapping)
        self.assertIn("📋 تیکت‌های من", mapping)

    def test_cancel_and_home_helpers(self):
        self.assertTrue(is_cancel_text("انصراف"))
        self.assertTrue(is_home_text("🏠 منوی اصلی", {"btn_menu_home": "🏠 منوی اصلی"}))
        self.assertTrue(is_home_text("شروع مجدد"))
        kb = cancel_reply()
        self.assertEqual(len(kb.keyboard), 1)
        self.assertNotIn("شروع مجدد", kb.keyboard[0][0].text)

    def test_admin_reply_has_hub_groups(self):
        ui = {
            "menu_layout": "compact",
            "btn_admin": "🛠 پنل",
            "btn_adm_orders": "🛒 سفارش‌ها",
            "btn_adm_users": "👥 کاربران",
            "btn_adm_settings": "⚙️ تنظیمات",
            "btn_adm_broadcast": "📢 پیام گروهی",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
        }
        main_flat = [b.text for row in main_reply_keyboard("admin", ui=ui).keyboard for b in row]
        # Option B: main menu only offers the panel entry; groups live in the hub.
        self.assertIn("🛠 پنل", main_flat)
        self.assertNotIn("🗓 عملیات روزانه", main_flat)
        self.assertNotIn("👥 کاربران", main_flat)
        flat = [b.text for row in admin_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("🗓 عملیات روزانه", flat)
        self.assertIn("👤 افراد", flat)
        self.assertIn("📦 محصول و پاسارگارد", flat)
        self.assertIn("🛠 سیستم", flat)
        self.assertNotIn("👥 کاربران", flat)
        self.assertNotIn("⚙️ تنظیمات", flat)
        self.assertNotIn("📢 پیام گروهی", flat)
        self.assertNotIn("📊 داشبورد", flat)


if __name__ == "__main__":
    unittest.main()
