"""Home/back: one welcome + lasting main ReplyKeyboard (Option B)."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class RenderHomeReplyLastTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_home_welcome_single_message_reply_kb(self):
        """Welcome is one message with lasting ReplyKeyboard (Open is MenuButton)."""
        from app.bot.handlers.start import render_home
        from app.db.models import Role

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = Role.USER.value
        db_user.full_name = "Test"
        db_user.telegram_id = 123
        db_user.username = "t"
        db_user.referred_by_id = None

        ui = {
            "nav_mode": "inline",
            "welcome_text": "سلام",
            "shop_title": "فروشگاه",
            "menu_order": "shop,wallet",
            "btn_menu_home": "🏠 منوی اصلی",
        }
        main_kb = MagicMock(name="MAIN_REPLY")

        with (
            patch(
                "app.bot.handlers.start.get_all_settings",
                new=AsyncMock(return_value=ui),
            ),
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.reseller_access.is_shop_owner_on_main_bot",
                return_value=False,
            ),
            patch(
                "app.bot.handlers.start._has_services",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                new=AsyncMock(return_value=(main_kb, ui, "user")),
            ),
            patch(
                "app.bot.handlers.start.seed_main_reply_kb",
                new=AsyncMock(),
            ) as seed,
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("HOME_TEXT", {}),
            ),
        ):
            await render_home(
                message,
                session,
                db_user,
                ui=ui,
                effective_role="user",
                is_reseller_bot=False,
            )

        self.assertEqual(message.answer.await_count, 1)
        call = message.answer.await_args
        self.assertEqual(call.args[0], "HOME_TEXT")
        self.assertIs(call.kwargs.get("reply_markup"), main_kb)
        seed.assert_not_awaited()

    async def test_render_home_with_seed_flag_still_attaches_reply(self):
        """seed_reply_kb no longer sends a filler; welcome itself carries ReplyKeyboard."""
        from app.bot.handlers.start import render_home
        from app.db.models import Role

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = Role.USER.value
        db_user.full_name = "Test"
        db_user.telegram_id = 123
        db_user.username = "t"
        db_user.referred_by_id = None

        ui = {
            "nav_mode": "inline",
            "welcome_text": "سلام",
            "shop_title": "فروشگاه",
            "menu_order": "shop,wallet",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_miniapp": "مینی اپ",
        }
        main_kb = MagicMock(name="MAIN_REPLY")

        with (
            patch(
                "app.bot.handlers.start.get_all_settings",
                new=AsyncMock(return_value=ui),
            ),
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.reseller_access.is_shop_owner_on_main_bot",
                return_value=False,
            ),
            patch(
                "app.bot.handlers.start._has_services",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                new=AsyncMock(return_value=(main_kb, ui, "user")),
            ),
            patch(
                "app.bot.handlers.start.seed_main_reply_kb",
                new=AsyncMock(),
            ) as seed,
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("HOME_TEXT", {}),
            ),
        ):
            await render_home(
                message,
                session,
                db_user,
                ui=ui,
                effective_role="user",
                is_reseller_bot=False,
                seed_reply_kb=True,
            )

        message.answer.assert_awaited_once()
        call = message.answer.await_args
        self.assertEqual(call.args[0], "HOME_TEXT")
        self.assertIs(call.kwargs.get("reply_markup"), main_kb)
        seed.assert_not_awaited()

    async def test_render_home_without_mini_still_attaches_reply(self):
        from app.bot.handlers.start import render_home
        from app.db.models import Role

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = Role.USER.value
        db_user.full_name = "Test"
        db_user.telegram_id = 123
        db_user.username = None

        ui = {"welcome_text": "سلام", "shop_title": "فروشگاه", "menu_order": "shop"}
        main_kb = MagicMock(name="MAIN_REPLY")

        with (
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.reseller_access.is_shop_owner_on_main_bot",
                return_value=False,
            ),
            patch(
                "app.bot.handlers.start._has_services",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                new=AsyncMock(return_value=(main_kb, ui, "user")),
            ),
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("HOME_TEXT", {}),
            ),
        ):
            await render_home(
                message,
                session,
                db_user,
                ui=ui,
                effective_role="user",
                is_reseller_bot=False,
            )

        message.answer.assert_awaited_once()
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), main_kb)


class HomeReplySourceGuards(unittest.TestCase):
    def test_render_home_welcome_paths_and_seed(self):
        from app.bot.handlers import start

        src = inspect.getsource(start.render_home)
        self.assertNotIn("send_reply_keyboard_last", src)
        self.assertNotIn("miniapp_inline_keyboard", src)
        self.assertNotIn("mini_markup", src)
        self.assertIn("message.answer(text, reply_markup=reply_kb", src)
        self.assertNotIn('await message.answer("📱"', src)
        self.assertNotIn("_miniapp_ahead_text", src)
        self.assertNotIn('tip: str = "⌨️"', inspect.getsource(start.seed_main_reply_kb))
        seed = inspect.getsource(start.seed_main_reply_kb)
        self.assertIn("attach_reply_keyboard", seed)
        self.assertNotIn("ephemeral=True", seed)

    def test_home_and_back_use_render_home(self):
        from app.bot.handlers import reply_nav

        home_src = inspect.getsource(reply_nav.reply_main_nav)
        self.assertIn("REPLY_ACTION_HOME", home_src)
        self.assertIn("render_home", home_src)
        back_src = inspect.getsource(reply_nav.handle_back)
        self.assertIn("render_home", back_src)


if __name__ == "__main__":
    unittest.main()
