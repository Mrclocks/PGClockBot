"""Mature hybrid nav: stable reply shortcuts + one live edited panel."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class PresentNavPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_edits_bot_message_in_place(self):
        from app.bot.nav_inline import present_nav_panel
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        message = AsyncMock()
        message.from_user = MagicMock(is_bot=True)
        message.message_id = 42
        message.chat = MagicMock(id=7)
        message.edit_text = AsyncMock()
        message.answer = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        inline = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="x", callback_data="nv:adm:ops")]
            ]
        )

        await present_nav_panel(
            message, text="ops", inline=inline, state=state
        )

        message.edit_text.assert_awaited()
        message.answer.assert_not_awaited()
        state.update_data.assert_awaited()

    async def test_answers_once_for_user_reply_and_remembers(self):
        from app.bot.nav_inline import (
            NAV_PANEL_CHAT_KEY,
            NAV_PANEL_MSG_KEY,
            present_nav_panel,
        )

        message = AsyncMock()
        message.from_user = MagicMock(is_bot=False)
        message.bot = MagicMock()
        sent = MagicMock()
        sent.chat = MagicMock(id=9)
        sent.message_id = 99
        message.answer = AsyncMock(return_value=sent)
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()

        await present_nav_panel(
            message, text="ops", inline=None, state=state
        )

        message.answer.assert_awaited()
        kwargs = state.update_data.await_args.kwargs
        self.assertEqual(kwargs.get(NAV_PANEL_CHAT_KEY), 9)
        self.assertEqual(kwargs.get(NAV_PANEL_MSG_KEY), 99)


class GroupHubBackCloseTests(unittest.TestCase):
    def test_ops_people_product_system_back_to_close(self):
        from app.bot.nav_inline import (
            admin_ops_hub_keyboard,
            admin_people_hub_keyboard,
            admin_product_hub_keyboard,
            admin_system_hub_keyboard,
        )

        for kb_fn in (
            admin_ops_hub_keyboard,
            lambda ui: admin_people_hub_keyboard(ui, can_manage_representatives=True),
            lambda ui: admin_product_hub_keyboard(ui, pg_features=frozenset({"pg_users"})),
            admin_system_hub_keyboard,
        ):
            data = [
                b.callback_data
                for row in kb_fn({}).inline_keyboard
                for b in row
            ]
            self.assertIn("nv:adm:close", data)
            self.assertNotIn("nv:adm:home", data)


class OpenAdminHomeNoDuplicateTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_home_does_not_send_groups_keyboard(self):
        from app.bot.handlers.reply_nav import open_admin_home

        message = AsyncMock()
        message.from_user = MagicMock(is_bot=False)
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        main_kb = MagicMock(name="MAIN")

        with (
            patch(
                "app.bot.handlers.reply_nav._deny_unless_owner",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.handlers.reply_nav.nav.set_nav_level",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "admin")),
            ),
            patch(
                "app.bot.nav_inline.admin_groups_hub_keyboard",
                return_value="GROUPS",
            ) as groups,
        ):
            await open_admin_home(message, session, db_user, state)

        groups.assert_not_called()
        markup = message.answer.await_args.kwargs.get("reply_markup")
        self.assertIs(markup, main_kb)


class WithInlineBackTests(unittest.TestCase):
    def test_appends_back_once(self):
        from app.bot.nav_inline import with_inline_back
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        base = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="a", callback_data="adm:dash")]
            ]
        )
        once = with_inline_back(base, {}, "nv:adm:ops")
        twice = with_inline_back(once, {}, "nv:adm:ops")
        data = [b.callback_data for row in twice.inline_keyboard for b in row]
        self.assertEqual(data.count("nv:adm:ops"), 1)


class NvAdmCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_clears_markup(self):
        from app.bot.handlers.nav_hubs import nv_adm_close

        callback = AsyncMock()
        callback.message = AsyncMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.update_data = AsyncMock()

        with (
            patch(
                "app.bot.handlers.reply_nav._deny_unless_owner",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.bot.nav_inline.safe_edit_inline",
                new_callable=AsyncMock,
                return_value=True,
            ) as edit,
        ):
            await nv_adm_close(
                callback, session, db_user, state, is_reseller_bot=False
            )

        edit.assert_awaited()
        self.assertIsNone(edit.await_args.kwargs.get("reply_markup"))


if __name__ == "__main__":
    unittest.main()
