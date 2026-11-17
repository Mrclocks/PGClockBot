"""3.6.4 — reply-only static menus; fix reseller/admin hubs; entity names stay inline."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class AdminResellersHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_adm_resellers_opens_hub_in_place_without_reply_kb(self):
        from app.bot.handlers.admin import adm_resellers

        callback = AsyncMock()
        callback.message = MagicMock()
        callback.message.answer = AsyncMock()
        state = AsyncMock()
        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch("app.bot.auth.is_platform_admin", return_value=True),
            patch("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=True)),
            patch("app.bot.handlers.admin._is_admin", MagicMock(return_value=True)),
            patch(
                "app.bot.handlers.reply_nav.open_admin_resellers_hub",
                new_callable=AsyncMock,
            ) as open_hub,
        ):
            await adm_resellers(
                callback, session=AsyncMock(), db_user=MagicMock(role="admin"), state=state
            )

        open_hub.assert_awaited_once()
        self.assertIs(open_hub.await_args.args[0], callback.message)
        self.assertFalse(open_hub.await_args.kwargs["push"])
        callback.message.answer.assert_not_awaited()

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


if __name__ == "__main__":
    unittest.main()
