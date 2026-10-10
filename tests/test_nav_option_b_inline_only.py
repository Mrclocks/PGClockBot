"""Option B: inline-only nav — settings labels, preview escape, plan Back."""

from __future__ import annotations

import unittest


class TestNavModeAlwaysInline(unittest.TestCase):
    def test_always_inline(self):
        from app.bot.nav_mode import is_inline_nav, nav_mode

        self.assertTrue(is_inline_nav(None))
        self.assertTrue(is_inline_nav({"nav_mode": "classic"}))
        self.assertEqual(nav_mode({"nav_mode": "classic"}), "inline")

    def test_default_settings_still_has_nav_mode_inline(self):
        from app.services.users import DEFAULT_SETTINGS

        self.assertEqual(DEFAULT_SETTINGS.get("nav_mode"), "inline")


class TestAdminHubSettingsLabels(unittest.TestCase):
    def test_custom_hub_labels(self):
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import (
            _reply_admin_hub_entries,
            _reply_admin_ops_entries,
            _reply_admin_people_entries,
            _reply_admin_system_entries,
        )

        ui = {
            "btn_adm_hub_ops": "AAA ops",
            "btn_adm_hub_people": "BBB people",
            "btn_adm_hub_product": "CCC product",
            "btn_adm_hub_system": "DDD system",
            "btn_adm_dash": "EEE dash",
            "btn_adm_reports": "FFF reports",
            "btn_adm_resellers": "GGG resellers",
            "btn_adm_loyalty": "HHH loyalty",
            "btn_adm_backup": "III backup",
        }
        texts = [t for _, t in _reply_admin_hub_entries(ui)]
        self.assertEqual(texts, ["AAA ops", "BBB people", "CCC product", "DDD system"])
        ops = [t for _, t in _reply_admin_ops_entries(ui)]
        self.assertIn("EEE dash", ops)
        self.assertIn("FFF reports", ops)
        people = [t for _, t in _reply_admin_people_entries(ui)]
        self.assertIn("GGG resellers", people)
        self.assertIn("HHH loyalty", people)
        system = [t for _, t in _reply_admin_system_entries(ui)]
        self.assertIn("III backup", system)


class TestPlanActionsBackAndLabel(unittest.TestCase):
    def test_plan_actions_back_and_buy_label(self):
        from app.bot.keyboards import plan_actions

        ui = {"btn_buy_continue": "خرید سفارشی", "btn_back": "بازگشت تست"}
        kb = plan_actions(42, ui)
        rows = kb.inline_keyboard
        self.assertEqual(rows[0][0].text, "خرید سفارشی")
        self.assertEqual(rows[0][0].callback_data, "shop:buy:42")
        self.assertEqual(rows[1][0].text, "بازگشت تست")
        self.assertEqual(rows[1][0].callback_data, "shop:list")

    def test_custom_confirm_back(self):
        from app.bot.keyboards import custom_confirm_keyboard

        ui = {"btn_buy_continue": "ادامه X", "btn_back": "بازگشت Y"}
        kb = custom_confirm_keyboard(ui)
        flat = [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]
        self.assertIn(("ادامه X", "shop:custom:buy"), flat)
        self.assertIn(("بازگشت Y", "shop:custom:gb:next"), flat)


class TestPreviewEscape(unittest.TestCase):
    def test_preview_exit_on_as_user_kb(self):
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import REPLY_ACTION_ADMIN, main_reply_keyboard

        ui = {
            "menu_order": "shop,wallet,support",
            "menu_layout": "compact",
            "btn_shop": "فروشگاه",
            "btn_wallet": "کیف",
            "btn_support": "پشتیبانی",
            "btn_adm_exit_preview": "خروج از پیش‌نمایش تست",
        }
        markup = main_reply_keyboard(
            "user",
            has_services=False,
            ui=ui,
            as_user=True,
            preview_exit_action=REPLY_ACTION_ADMIN,
        )
        labels = [btn.text for row in markup.keyboard for btn in row]
        self.assertIn("خروج از پیش‌نمایش تست", labels)
        self.assertIn("فروشگاه", labels)
        self.assertNotIn("🗓 عملیات روزانه", labels)


class TestResellerThinReply(unittest.TestCase):
    def test_thin_hub(self):
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import reseller_hub_main_keyboard

        ui = {
            "menu_layout": "compact",
            "btn_reseller": "مدیریت من",
            "btn_adm_preview": "پیش‌نمایش من",
        }
        markup = reseller_hub_main_keyboard(None, ui)
        labels = [btn.text for row in markup.keyboard for btn in row]
        self.assertEqual(set(labels), {"مدیریت من", "پیش‌نمایش من"})


if __name__ == "__main__":
    unittest.main()
