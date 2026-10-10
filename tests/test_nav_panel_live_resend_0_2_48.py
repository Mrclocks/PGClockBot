"""Tracked panel: edit only while live; resend at bottom after newer messages."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class PanelIsStillLiveTests(unittest.TestCase):
    def test_intervening_message_means_stale(self):
        """panel=40, notice=41, user tap=42 → gap → must resend, not edit."""
        from app.bot.nav_inline import panel_is_still_live

        self.assertFalse(panel_is_still_live(MagicMock(message_id=42), 40))
        self.assertFalse(panel_is_still_live(MagicMock(message_id=50), 40))

    def test_immediate_reply_tap_is_still_live(self):
        """panel=40, user Back=41 with nothing in between → edit in place."""
        from app.bot.nav_inline import panel_is_still_live

        self.assertTrue(panel_is_still_live(MagicMock(message_id=41), 40))
        self.assertTrue(panel_is_still_live(MagicMock(message_id=40), 40))
        self.assertTrue(panel_is_still_live(MagicMock(message_id=30), 40))

    def test_none_tracked_is_not_live(self):
        from app.bot.nav_inline import panel_is_still_live

        self.assertFalse(panel_is_still_live(MagicMock(message_id=10), None))


class PresentNavPanelStaleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_tracked_panel_answers_new_at_bottom(self):
        """Preview/notice after panel → Back must answer a fresh hub below."""
        from app.bot.nav_inline import NAV_PANEL_CHAT_KEY, NAV_PANEL_MSG_KEY, present_nav_panel

        message = MagicMock()
        message.from_user = MagicMock(is_bot=False)
        message.message_id = 100  # gap after tracked 50 (notice in between)
        message.chat = MagicMock(id=7)
        message.bot = MagicMock()
        message.bot.edit_message_text = AsyncMock()
        sent = MagicMock(message_id=101, chat=MagicMock(id=7))
        message.answer = AsyncMock(return_value=sent)
        state = AsyncMock()
        state.get_data = AsyncMock(
            return_value={NAV_PANEL_CHAT_KEY: 7, NAV_PANEL_MSG_KEY: 50}
        )
        state.update_data = AsyncMock()

        out = await present_nav_panel(
            message, text="HUB", inline="INLINE", state=state
        )

        message.bot.edit_message_text.assert_not_awaited()
        message.answer.assert_awaited_once()
        self.assertIs(out, sent)
        # Stale tracker cleared so we do not keep editing history later.
        clear_kwargs = state.update_data.await_args_list[0].kwargs
        self.assertIn(NAV_PANEL_MSG_KEY, clear_kwargs)
        self.assertIsNone(clear_kwargs[NAV_PANEL_MSG_KEY])

    async def test_immediate_reply_tap_edits_tracked_panel(self):
        """User Back right after panel (id = tracked+1) edits in place."""
        from app.bot.nav_inline import NAV_PANEL_CHAT_KEY, NAV_PANEL_MSG_KEY, present_nav_panel

        message = MagicMock()
        message.from_user = MagicMock(is_bot=False)
        message.message_id = 51  # immediate tap after tracked 50
        message.chat = MagicMock(id=7)
        message.bot = MagicMock()
        message.bot.edit_message_text = AsyncMock()
        message.answer = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(
            return_value={NAV_PANEL_CHAT_KEY: 7, NAV_PANEL_MSG_KEY: 50}
        )

        out = await present_nav_panel(
            message, text="HUB", inline="INLINE", state=state
        )

        message.bot.edit_message_text.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.assertIsNone(out)

    async def test_same_id_tracked_panel_edits_in_place(self):
        from app.bot.nav_inline import NAV_PANEL_CHAT_KEY, NAV_PANEL_MSG_KEY, present_nav_panel

        message = MagicMock()
        message.from_user = MagicMock(is_bot=False)
        message.message_id = 50
        message.chat = MagicMock(id=7)
        message.bot = MagicMock()
        message.bot.edit_message_text = AsyncMock()
        message.answer = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(
            return_value={NAV_PANEL_CHAT_KEY: 7, NAV_PANEL_MSG_KEY: 50}
        )

        out = await present_nav_panel(
            message, text="HUB", inline="INLINE", state=state
        )

        message.bot.edit_message_text.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.assertIsNone(out)

    async def test_callback_on_panel_still_edits(self):
        """Inline tap on the panel bubble must edit — even if ids look 'newer'."""
        from app.bot.nav_inline import present_nav_panel

        message = MagicMock()
        message.from_user = MagicMock(is_bot=True)
        message.message_id = 99
        message.answer = AsyncMock()
        state = AsyncMock()
        state.update_data = AsyncMock()

        with patch(
            "app.bot.nav_inline.safe_edit_inline",
            AsyncMock(return_value=True),
        ) as edit:
            out = await present_nav_panel(
                message, text="LEAF", inline="INLINE", state=state
            )

        edit.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.assertIs(out, message)


class PreviewExitResendTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_exit_heals_keyboard_and_clears_panel(self):
        from app.bot.handlers.reply_nav import reply_main_nav
        from app.bot import keyboards as kb
        from app.bot import menu_nav as nav

        message = AsyncMock()
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})

        with (
            patch(
                "app.bot.handlers.reply_nav.nav.get_nav_level",
                AsyncMock(return_value=nav.NAV_USER_PREVIEW),
            ),
            patch(
                "app.bot.nav_inline.clear_nav_panel",
                AsyncMock(),
            ) as clear,
            patch(
                "app.bot.nav_chrome.heal_main_reply",
                AsyncMock(),
            ) as heal,
            patch(
                "app.bot.handlers.reply_nav.open_admin_home",
                AsyncMock(),
            ) as home,
            patch(
                "app.bot.handlers.reply_nav._deny_unless_owner",
                AsyncMock(return_value=True),
            ),
        ):
            await reply_main_nav(
                message,
                session,
                db_user,
                state,
                reply_action=kb.REPLY_ACTION_ADMIN,
                reply_ui={},
                reply_role="admin",
                is_reseller_bot=False,
            )

        clear.assert_awaited()
        heal.assert_awaited()
        home.assert_awaited()


if __name__ == "__main__":
    unittest.main()
