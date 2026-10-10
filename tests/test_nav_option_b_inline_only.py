"""Option B: inline-only nav — settings labels, preview escape, plan Back."""

from __future__ import annotations

import unittest

import app.bot.keyboards  # noqa: F401 — resolve reply_keyboards circular init

from app.bot.keyboards import custom_confirm_keyboard, plan_actions
from app.bot.nav_inline import admin_groups_hub_keyboard
from app.bot.nav_mode import is_inline_nav, nav_mode
from app.bot.reply_keyboards import (
    REPLY_ACTION_ADMIN,
    main_reply_keyboard,
    reseller_hub_main_keyboard,
)


class TestNavModeAlwaysInline(unittest.TestCase):
    def test_always_inline(self):
        self.assertTrue(is_inline_nav(None))
        self.assertTrue(is_inline_nav({"nav_mode": "classic"}))
        self.assertEqual(nav_mode({"nav_mode": "classic"}), "inline")


class TestAdminHubSettingsLabels(unittest.TestCase):
    def test_custom_hub_labels(self):
        ui = {
            "btn_adm_hub_ops": "AAA ops",
            "btn_adm_hub_people": "BBB people",
            "btn_adm_hub_product": "CCC product",
            "btn_adm_hub_system": "DDD system",
        }
        labels = [
            b.text for row in admin_groups_hub_keyboard(ui).inline_keyboard for b in row
        ]
        self.assertEqual(
            labels[:4],
            ["AAA ops", "BBB people", "CCC product", "DDD system"],
        )


class TestPlanActionsBackAndLabel(unittest.TestCase):
    def test_plan_actions_back_and_buy_label(self):
        ui = {"btn_buy_continue": "خرید سفارشی", "btn_back": "بازگشت تست"}
        kb = plan_actions(42, ui)
        rows = kb.inline_keyboard
        self.assertEqual(rows[0][0].text, "خرید سفارشی")
        self.assertEqual(rows[0][0].callback_data, "shop:buy:42")
        self.assertEqual(rows[1][0].text, "بازگشت تست")
        self.assertEqual(rows[1][0].callback_data, "shop:list")

    def test_custom_confirm_buy_label(self):
        ui = {"btn_buy_continue": "ادامه X", "btn_back": "بازگشت Y"}
        kb = custom_confirm_keyboard(ui)
        flat = [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]
        self.assertIn(("ادامه X", "shop:custom:buy"), flat)


class TestPreviewEscape(unittest.TestCase):
    def test_preview_exit_on_as_user_kb(self):
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

    def test_exit_label_maps_when_merging_user_preview_into_admin(self):
        """Filter merges role=user as_user=True into admin map — exit must resolve."""
        from app.bot.reply_keyboards import reply_action_map
        from app.services.users import DEFAULT_SETTINGS

        ui = dict(DEFAULT_SETTINGS)
        exit_label = ui["btn_adm_exit_preview"]
        mapping = reply_action_map(
            "admin",
            has_services=True,
            ui=ui,
            include_submenus=True,
            is_reseller_bot=False,
        )
        for k, v in reply_action_map(
            "user",
            has_services=True,
            ui=ui,
            as_user=True,
            include_submenus=True,
            is_reseller_bot=False,
        ).items():
            mapping.setdefault(k, v)
        self.assertEqual(mapping.get(exit_label), "admin")
        # Direct as_user with role=user must also register (regression of role check)
        direct = reply_action_map(
            "user", has_services=True, ui=ui, as_user=True, is_reseller_bot=False
        )
        self.assertEqual(direct.get(exit_label), "admin")


class TestResellerThinReply(unittest.TestCase):
    def test_thin_hub_from_settings(self):
        ui = {
            "menu_layout": "compact",
            "btn_reseller": "مدیریت من",
            "btn_adm_preview": "پیش‌نمایش من",
        }
        markup = reseller_hub_main_keyboard(None, ui)
        labels = [btn.text for row in markup.keyboard for btn in row]
        self.assertEqual(set(labels), {"مدیریت من", "پیش‌نمایش من"})
        self.assertEqual(len(labels), 2)


if __name__ == "__main__":
    unittest.main()
