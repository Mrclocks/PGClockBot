"""Option B: inline-only nav — settings labels, preview escape, plan Back."""

from __future__ import annotations

import unittest

import app.bot.keyboards  # noqa: F401 — resolve reply_keyboards circular init

from app.bot.keyboards import custom_days_keyboard, plan_actions, wholesale_qty_keyboard
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
            "btn_adm_hub_marketing": "EEE marketing",
            "btn_adm_hub_system": "DDD system",
        }
        labels = [
            b.text for row in admin_groups_hub_keyboard(ui).inline_keyboard for b in row
        ]
        self.assertEqual(
            labels[:5],
            ["AAA ops", "BBB people", "CCC product", "EEE marketing", "DDD system"],
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

    def test_custom_and_wholesale_continue_straight_to_payment(self):
        ui = {"btn_buy_continue": "ادامه X", "btn_back": "بازگشت Y"}
        for name, markup, callback in (
            ("custom", custom_days_keyboard(30, ui), "shop:custom:confirm"),
            ("wholesale", wholesale_qty_keyboard(5, ui, plan_id=3), "shop:wholesale:confirm"),
        ):
            with self.subTest(name):
                flat = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
                self.assertIn(("ادامه X", callback), flat)


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
        markup = reseller_hub_main_keyboard(None, {**ui, "show_restart_button": "0"})
        labels = [btn.text for row in markup.keyboard for btn in row]
        self.assertEqual(set(labels), {"مدیریت من", "پیش‌نمایش من"})
        self.assertEqual(len(labels), 2)

    def test_thin_hub_adds_only_restart_footer_when_enabled(self):
        ui = {"btn_reseller": "مدیریت من", "btn_adm_preview": "پیش‌نمایش من"}
        markup = reseller_hub_main_keyboard(None, {**ui, "show_restart_button": "1"})
        rows = [[btn.text for btn in row] for row in markup.keyboard]
        self.assertEqual(rows[0], ["مدیریت من", "پیش‌نمایش من"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[1]), 1)


class TestStickyUserPreviewFlag(unittest.IsolatedAsyncioTestCase):
    async def test_preview_flag_survives_nested_level_change(self):
        from unittest.mock import AsyncMock

        from app.bot import menu_nav as nav

        state = AsyncMock()
        store: dict = {}

        async def _get():
            return dict(store)

        async def _update(**kw):
            store.update(kw)

        state.get_data = _get
        state.update_data = _update

        await nav.set_nav_level(state, nav.NAV_USER_PREVIEW, push=True)
        await nav.set_user_preview_active(state, True)
        self.assertTrue(await nav.is_user_preview_active(state))

        # Nested shop tap inside preview changes level but must keep sticky flag.
        await nav.set_nav_level(state, nav.NAV_SHOP, push=True)
        self.assertEqual(await nav.get_nav_level(state), nav.NAV_SHOP)
        self.assertTrue(await nav.is_user_preview_active(state))

        await nav.set_user_preview_active(state, False)
        self.assertFalse(await nav.is_user_preview_active(state))

    def test_admin_exit_uses_sticky_preview_flag(self):
        import inspect

        from app.bot.handlers import reply_nav as rn

        src = inspect.getsource(rn)
        self.assertIn("is_user_preview_active", src)
        self.assertIn("set_user_preview_active", src)


class TestGiftExpiresWidthCss(unittest.TestCase):
    def test_gift_expires_uses_plain_text_input(self):
        """datetime-local crushed on mobile WebViews — use text like siblings."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        css = (root / "app/web/static/panel.css").read_text(encoding="utf-8")
        gift = (root / "app/web/templates/_gift_code_fields.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('name="expires_at"', gift)
        self.assertRegex(gift, r'name="expires_at"[\s\S]*?type="text"')
        self.assertNotIn('type="datetime-local"', gift)
        self.assertNotIn("gift-expires-input-wrap", gift)
        self.assertNotIn("datetime-local", css)
        self.assertIn("gift-expires-field", css)
        self.assertIn("<small class=\"muted\">", gift)


if __name__ == "__main__":
    unittest.main()
