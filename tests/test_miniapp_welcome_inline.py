"""Miniapp: MenuButton Open only — welcome carries lasting ReplyKeyboard."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.keyboards import DEFAULT_MENU_ORDER, REMOVED_MENU_KEYS, _menu_order
from app.services.users import DEFAULT_SETTINGS

ROOT = Path(__file__).resolve().parents[1]


class MiniappMenuCatalogTests(unittest.TestCase):
    def test_miniapp_not_in_default_menu_order(self):
        self.assertNotIn("miniapp", DEFAULT_MENU_ORDER)
        self.assertIn("miniapp", REMOVED_MENU_KEYS)
        active = [
            p.strip()
            for p in str(DEFAULT_SETTINGS.get("menu_order") or "").split(",")
            if p.strip()
        ]
        self.assertNotIn("miniapp", active)

    def test_menu_order_strips_legacy_miniapp(self):
        ordered = _menu_order({"menu_order": "shop,miniapp,wallet"})
        self.assertEqual(ordered, ["shop", "wallet"])

    def test_no_miniapp_inline_keyboard_helper(self):
        import app.bot.keyboards as kb

        self.assertFalse(hasattr(kb, "miniapp_inline_keyboard"))
        self.assertFalse(hasattr(kb, "miniapp_reseller_keyboard"))
        src = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertNotIn("def miniapp_inline_keyboard", src)
        self.assertNotIn("WebAppInfo", src)


class RenderHomeOpenOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def _run_home(self, *, role: str, is_reseller_bot: bool, main_kb, ui):
        from app.bot.handlers.start import render_home

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = role
        db_user.full_name = "Test"
        db_user.telegram_id = 123
        db_user.username = "t"

        with (
            patch(
                "app.bot.handlers.start.get_all_settings",
                new=AsyncMock(return_value=ui),
            ),
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value=role),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                new=AsyncMock(return_value=(main_kb, ui, role)),
            ),
            patch(
                "app.services.rich_text.outbound_setting_text",
                return_value=("HOME_TEXT", {}),
            ),
            patch(
                "app.bot.handlers.start.seed_main_reply_kb",
                new=AsyncMock(),
            ) as seed,
        ):
            await render_home(
                message,
                session,
                db_user,
                ui=ui,
                effective_role=role,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=9 if is_reseller_bot else None,
                seed_reply_kb=True,
            )
        return message, seed

    async def test_user_welcome_carries_reply_kb(self):
        ui = {"welcome_text": "سلام", "shop_title": "فروشگاه", "btn_miniapp": "مینی اپ"}
        main_kb = MagicMock(name="MAIN_REPLY")
        message, seed = await self._run_home(
            role="user",
            is_reseller_bot=False,
            main_kb=main_kb,
            ui=ui,
        )
        message.answer.assert_awaited_once()
        call = message.answer.await_args
        self.assertEqual(call.args[0], "HOME_TEXT")
        self.assertIs(call.kwargs.get("reply_markup"), main_kb)
        seed.assert_not_awaited()

    async def test_admin_welcome_carries_reply_kb(self):
        ui = {"shop_title": "فروشگاه", "btn_miniapp": "مینی اپ"}
        main_kb = MagicMock(name="MAIN_REPLY")
        message, seed = await self._run_home(
            role="admin",
            is_reseller_bot=False,
            main_kb=main_kb,
            ui=ui,
        )
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), main_kb)
        seed.assert_not_awaited()

    async def test_reseller_on_platform_bot_gets_reply_kb(self):
        ui = {"shop_title": "فروشگاه", "btn_miniapp": "مینی اپ"}
        main_kb = MagicMock(name="MAIN_REPLY")
        message, seed = await self._run_home(
            role="reseller",
            is_reseller_bot=False,
            main_kb=main_kb,
            ui=ui,
        )
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), main_kb)
        seed.assert_not_awaited()

    async def test_shop_bot_also_gets_reply_kb(self):
        ui = {"welcome_text": "سلام", "shop_title": "فروشگاه", "menu_order": "shop"}
        main_kb = MagicMock(name="MAIN_REPLY")
        message, seed = await self._run_home(
            role="user",
            is_reseller_bot=True,
            main_kb=main_kb,
            ui=ui,
        )
        seed.assert_not_awaited()
        message.answer.assert_awaited_once()
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), main_kb)

    async def test_home_is_a_new_message_with_the_menu_and_no_inline_markup(self):
        from app.bot.handlers.start import render_home

        message = AsyncMock()
        message.edit_text = AsyncMock(return_value=MagicMock())
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = "user"
        db_user.full_name = "Test"
        db_user.telegram_id = 123
        db_user.username = "t"
        ui = {"welcome_text": "سلام", "shop_title": "فروشگاه"}
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
            )

        message.edit_text.assert_not_awaited()  # a home is a fresh start, never an edited panel
        message.answer.assert_awaited_once()
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), main_kb)


class MenuButtonOpenPreservedTests(unittest.TestCase):
    def test_chat_menu_still_sets_webapp_open(self):
        src = (ROOT / "app/bot/chat_menu.py").read_text(encoding="utf-8")
        self.assertIn("MenuButtonWebApp", src)
        self.assertIn("sync_telegram_menu_button", src)
        self.assertIn("WebAppInfo", src)
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("sync_telegram_menu_button", main)

    def test_render_home_has_no_inline_miniapp(self):
        from app.bot.handlers import start

        src = inspect.getsource(start.render_home)
        self.assertNotIn("miniapp_inline_keyboard", src)
        self.assertNotIn("mini_markup", src)
        self.assertIn("reply_markup=reply_kb", src)


class SeedMainReplyNoKeyboardEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def test_seed_main_reply_kb_rejects_keyboard_emoji_tip(self):
        from app.bot.handlers.start import seed_main_reply_kb

        message = AsyncMock()
        reply_kb = MagicMock()
        with patch(
            "app.bot.tg_utils.attach_reply_keyboard",
            new=AsyncMock(),
        ) as attach:
            await seed_main_reply_kb(message, reply_kb, tip="⌨️")
            attach.assert_awaited_once()
            self.assertNotIn("⌨️", attach.await_args.kwargs.get("text", ""))
            self.assertEqual(attach.await_args.kwargs.get("text"), "منوی اصلی")


if __name__ == "__main__":
    unittest.main()
