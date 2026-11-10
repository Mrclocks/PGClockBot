"""Admin «بازاریابی» hub: club and broadcast live behind one Owner-only group."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot import keyboards as kb
from app.bot import menu_nav as nav
from app.bot import reply_keyboards as rk
from app.bot.nav_inline import (
    admin_broadcast_hub_keyboard,
    admin_groups_hub_keyboard,
    admin_loyalty_manage_hub_keyboard,
    admin_marketing_hub_keyboard,
    admin_people_hub_keyboard,
    admin_system_hub_keyboard,
)
from app.services.users import DEFAULT_SETTINGS


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class MarketingHubKeyboardTests(unittest.TestCase):
    def test_hub_group_sits_between_product_and_system(self):
        groups = _callbacks(admin_groups_hub_keyboard({}))
        self.assertEqual(
            groups,
            ["nv:adm:ops", "nv:adm:people", "nv:adm:product", "nv:adm:marketing", "nv:adm:system", "cma:home:0", "ed:a:h", "menu:home"],
        )

    def test_hub_lists_its_leaves_and_returns_home(self):
        self.assertEqual(
            _callbacks(admin_marketing_hub_keyboard({})),
            [
                "nv:adm:loy",
                "nv:adm:broadcast",
                "nv:adm:gc",
                "nv:adm:cmp",
                "nv:adm:cb",
                "tru:l:all:0",
                "nv:adm:home",
            ],
        )

    def test_club_and_broadcast_left_their_old_hubs(self):
        self.assertNotIn("nv:adm:loy", _callbacks(admin_people_hub_keyboard({})))
        self.assertNotIn("nv:adm:broadcast", _callbacks(admin_system_hub_keyboard({})))

    def test_leaf_hubs_return_to_the_marketing_hub(self):
        self.assertIn("nv:adm:marketing", _callbacks(admin_broadcast_hub_keyboard({})))
        self.assertIn("nv:adm:marketing", _callbacks(admin_loyalty_manage_hub_keyboard({})))

    def test_admin_can_rename_the_group_button(self):
        ui = {"btn_adm_hub_marketing": "رشد"}
        self.assertIn("رشد", _labels(admin_groups_hub_keyboard(ui)))

    def test_default_label_exists_so_existing_installs_render_it(self):
        self.assertEqual(DEFAULT_SETTINGS["btn_adm_hub_marketing"], "🎯 بازاریابی")

    def test_callback_data_stays_within_telegram_limit(self):
        for markup in (admin_groups_hub_keyboard({}), admin_marketing_hub_keyboard({})):
            for data in _callbacks(markup):
                self.assertLessEqual(len(data.encode()), 64)


class MarketingReplyRoutingTests(unittest.TestCase):
    def test_every_admin_hub_and_leaf_label_is_unique(self):
        ui = dict(DEFAULT_SETTINGS)
        labels = [
            label.strip()
            for entries in (
                kb._reply_admin_hub_entries(ui),
                kb._reply_admin_ops_entries(ui),
                kb._reply_admin_people_entries(ui),
                kb._reply_admin_product_entries(ui),
                rk._reply_admin_marketing_entries(ui),
                kb._reply_admin_system_entries(ui),
            )
            for _, label in entries
        ]
        self.assertEqual(len(labels), len(set(labels)))

    def test_flat_map_resolves_the_group_and_its_leaves_for_admins_only(self):
        ui = dict(DEFAULT_SETTINGS)
        admin = kb.reply_action_map("admin", ui=ui)
        self.assertEqual(admin["🎯 بازاریابی"], "adm_hub_marketing")
        self.assertEqual(admin["⭐ باشگاه مشتریان"], "adm_loyalty")
        self.assertEqual(admin["📢 پیام گروهی"], "adm_broadcast")
        for role, reseller_bot in (("user", False), ("reseller", True), ("admin", True)):
            mapping = kb.reply_action_map(role, ui=ui, is_reseller_bot=reseller_bot)
            self.assertNotIn("🎯 بازاریابی", mapping, (role, reseller_bot))
            self.assertNotIn("📢 پیام گروهی", mapping, (role, reseller_bot))

    def test_marketing_reply_keyboard_lists_only_its_leaves(self):
        flat = [b.text for row in rk.admin_marketing_reply_keyboard(dict(DEFAULT_SETTINGS)).keyboard for b in row]
        self.assertIn("⭐ باشگاه مشتریان", flat)
        self.assertIn("📢 پیام گروهی", flat)
        self.assertNotIn("⚙️ تنظیمات", flat)
        self.assertNotIn("👥 کاربران", flat)


class OpenMarketingHubTests(unittest.IsolatedAsyncioTestCase):
    async def _open(self, *, owner: bool, reseller_bot: bool = False):
        from app.bot.handlers.nav_marketing import open_admin_marketing_hub

        message = AsyncMock()
        state = AsyncMock()
        present = AsyncMock()
        with (
            patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=owner)),
            patch("app.bot.handlers.nav_marketing.get_all_settings", AsyncMock(return_value=dict(DEFAULT_SETTINGS))),
            patch("app.bot.nav_inline.present_nav_panel", present),
            patch("app.bot.handlers.nav_marketing.nav.set_nav_level", AsyncMock()) as set_level,
        ):
            await open_admin_marketing_hub(
                message, MagicMock(), MagicMock(), state, is_reseller_bot=reseller_bot
            )
        return message, present, set_level

    async def test_owner_sees_the_hub_and_the_level_is_recorded(self):
        message, present, set_level = await self._open(owner=True)
        present.assert_awaited_once()
        self.assertIn("nv:adm:loy", _callbacks(present.await_args.kwargs["inline"]))
        set_level.assert_awaited_once()
        self.assertEqual(set_level.await_args.args[1], nav.NAV_ADMIN_MARKETING)

    async def test_non_owner_is_refused_and_sees_no_hub(self):
        message, present, set_level = await self._open(owner=False)
        present.assert_not_awaited()
        set_level.assert_not_awaited()
        message.answer.assert_awaited()

    async def test_shop_bot_never_opens_the_platform_hub(self):
        message, present, set_level = await self._open(owner=True, reseller_bot=True)
        present.assert_not_awaited()
        set_level.assert_not_awaited()
        message.answer.assert_awaited()


class MarketingCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_opens_the_hub_without_pushing_history(self):
        from app.bot.handlers import nav_marketing

        callback = AsyncMock()
        callback.message = AsyncMock()
        with patch.object(nav_marketing, "open_admin_marketing_hub", AsyncMock()) as opener:
            await nav_marketing.nv_adm_marketing(callback, MagicMock(), MagicMock(), AsyncMock())
        opener.assert_awaited_once()
        self.assertFalse(opener.await_args.kwargs["push"])

    async def test_callback_without_message_is_ignored(self):
        from app.bot.handlers import nav_marketing

        callback = AsyncMock()
        callback.message = None
        with patch.object(nav_marketing, "open_admin_marketing_hub", AsyncMock()) as opener:
            await nav_marketing.nv_adm_marketing(callback, MagicMock(), MagicMock(), AsyncMock())
        opener.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
