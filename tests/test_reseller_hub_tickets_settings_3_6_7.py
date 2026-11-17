"""3.6.7 — reseller hub on shop /start; ticket manage; shop settings completeness."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class ResellerHubStartTests(unittest.TestCase):

    def test_hub_keyboard_has_preview(self):
        import app.bot.keyboards as kb

        ui: dict = {"show_restart_button": "0"}
        labels = [
            b.text for row in kb.reseller_hub_main_keyboard(None, ui).keyboard for b in row
        ]
        preview_label = "👁 پیش‌نمایش منوی کاربر"
        self.assertIn(preview_label, labels)
        mapping = kb.reply_action_map(
            "reseller",
            ui=ui,
            include_submenus=True,
            is_reseller_bot=True,
            profile=None,
        )
        self.assertEqual(mapping[preview_label], kb.REPLY_ACTION_RES_PREVIEW)


class UserPreviewKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_forces_user_role(self):
        from app.bot import menu_nav as nav

        message = MagicMock()
        message.answer = AsyncMock()
        db_user = MagicMock(id=3)
        ui = {
            "btn_admin": "🛠 پنل ادمین",
            "btn_adm_exit_preview": "↩️ خروج از پیش‌نمایش",
            "show_restart_button": "0",
        }
        with (
            patch.object(nav, "get_all_settings", AsyncMock(return_value=ui)),
            patch.object(nav, "user_has_services", AsyncMock(return_value=False)),
        ):
            await nav.show_nav_keyboard(
                message,
                AsyncMock(),
                db_user,
                nav.NAV_USER_PREVIEW,
                text="پیش‌نمایش",
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        markup = message.answer.await_args.kwargs["reply_markup"]
        labels = [b.text for row in markup.keyboard for b in row]
        self.assertIn("↩️ خروج از پیش‌نمایش", labels)
        self.assertNotIn("🛠 پنل ادمین", labels)
        self.assertNotIn("🗓 عملیات روزانه", labels)
        self.assertIn("🟢🛒 خرید سرویس", labels)


class TicketManageTests(unittest.IsolatedAsyncioTestCase):
    async def test_res_tickets_has_action_buttons(self):
        from app.bot.handlers import reseller

        ticket = MagicMock(id=5, subject="مشکل اتصال")
        customer = MagicMock(full_name="Ali", username=None, telegram_id=1)
        result = MagicMock()
        result.all.return_value = [(ticket, customer)]
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        callback = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        with (
            patch.object(
                reseller, "_actor", AsyncMock(return_value=(9, MagicMock()))
            ),
            patch.object(reseller, "has_bot_perm", MagicMock(return_value=True)),
            patch("app.services.users.get_all_settings", AsyncMock(return_value={})),
        ):
            await reseller.res_tickets(callback, session, MagicMock())

        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("tkt:reply:5", callbacks)
        self.assertIn("tkt:close:5", callbacks)

    def test_with_shop_settings_keeps_home_only(self):
        from app.services.resellers import with_shop_settings

        # Empty / intentional subsets must not be force-expanded to full shop ACL.
        perms = with_shop_settings([])
        self.assertEqual(perms, ["dashboard"])
        subset = with_shop_settings(["tickets"])
        self.assertEqual(subset, ["tickets", "dashboard"])
        self.assertNotIn("shop_settings", subset)
        self.assertNotIn("plans", subset)


class ShopSettingsCompletenessTests(unittest.TestCase):
    def test_naming_tab_for_reseller(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS

        keys = {k for k, _ in RESELLER_SETTINGS_TABS}
        self.assertIn("menu", keys)
        self.assertIn("naming", keys)
        self.assertIn("buttons", keys)

    def test_shop_buttons_filter_platform_labels(self):
        src = (ROOT / "app/api/shop_settings.py").read_text(encoding="utf-8")
        self.assertIn("PLATFORM_ONLY_BTN_KEYS", src)
        from app.services.settings_button_labels import PLATFORM_ONLY_BTN_KEYS

        self.assertIn("btn_admin", PLATFORM_ONLY_BTN_KEYS)
        self.assertIn("btn_reseller_apply", PLATFORM_ONLY_BTN_KEYS)
        self.assertNotIn("btn_miniapp", PLATFORM_ONLY_BTN_KEYS)
        self.assertIn("btn_adm_users", PLATFORM_ONLY_BTN_KEYS)


if __name__ == "__main__":
    unittest.main()
