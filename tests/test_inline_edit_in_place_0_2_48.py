"""Inline taps must edit the same bot message — no filler chrome spam."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _bot_message() -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(is_bot=True)
    message.answer = AsyncMock(return_value=MagicMock())
    message.edit_text = AsyncMock()
    message.chat = MagicMock(id=7)
    message.message_id = 99
    message.bot = MagicMock()
    return message


def _user_message() -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(is_bot=False)
    message.answer = AsyncMock(return_value=MagicMock())
    message.edit_text = AsyncMock()
    return message


class PresentInlineOnlyEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_message_edits_in_place(self):
        from app.bot.nav_inline import present_inline_only

        message = _bot_message()
        with patch(
            "app.bot.nav_inline.safe_edit_inline",
            AsyncMock(return_value=True),
        ) as edit:
            out = await present_inline_only(
                message, text="BODY", inline="INLINE"
            )
        edit.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.assertIs(out, message)

    async def test_user_message_answers_once(self):
        from app.bot.nav_inline import present_inline_only

        message = _user_message()
        await present_inline_only(message, text="BODY", inline="INLINE")
        self.assertEqual(message.answer.await_count, 1)
        message.edit_text.assert_not_awaited()


class NoFillerChromeTests(unittest.TestCase):
    def test_filler_string_removed_from_handlers(self):
        from pathlib import Path

        banned = "از منوی پایین یا دکمه‌های بالا ادامه دهید."
        roots = [
            Path("app/bot/handlers/loyalty.py"),
            Path("app/bot/handlers/admin_plans.py"),
        ]
        for path in roots:
            src = path.read_text(encoding="utf-8")
            self.assertNotIn(banned, src, msg=str(path))


class StaffLoyaltyAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_edits_without_filler_answer(self):
        from app.bot.handlers.loyalty import _staff_loyalty_answer

        message = _bot_message()
        session = AsyncMock()
        db_user = MagicMock()

        with (
            patch(
                "app.services.users.get_all_settings",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.bot.nav_inline.present_inline_only",
                AsyncMock(return_value=message),
            ) as present,
            patch(
                "app.bot.nav_chrome.heal_main_reply",
                AsyncMock(),
            ) as heal,
        ):
            await _staff_loyalty_answer(
                message,
                session,
                db_user,
                "🎖 سطوح باشگاه",
                can_tiers=True,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        present.assert_awaited_once()
        heal.assert_not_awaited()
        # No second chrome answer after the panel
        message.answer.assert_not_awaited()

    async def test_fsm_cancel_heals_then_reopens_settings(self):
        from app.bot.handlers.loyalty import _staff_loyalty_answer

        message = _user_message()
        session = AsyncMock()
        db_user = MagicMock()
        inline = MagicMock()

        with (
            patch(
                "app.services.users.get_all_settings",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.bot.nav_inline.present_inline_only",
                AsyncMock(return_value=None),
            ) as present,
            patch(
                "app.bot.nav_chrome.heal_main_reply",
                AsyncMock(),
            ) as heal,
        ):
            await _staff_loyalty_answer(
                message,
                session,
                db_user,
                "⚙️ تنظیمات",
                can_tiers=True,
                content_inline=inline,
                heal_reply=True,
            )

        heal.assert_awaited_once()
        present.assert_awaited_once()


class SyncPlansNoFillerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_plans_only_presents_panel(self):
        from app.bot.handlers.admin_plans import sync_plans_reply_keyboard

        message = _bot_message()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()

        with (
            patch(
                "app.bot.handlers.admin_plans.get_all_settings",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.bot.handlers.admin_plans.nav.set_nav_level",
                AsyncMock(),
            ),
            patch(
                "app.bot.nav_inline.present_inline_only",
                AsyncMock(),
            ) as present,
        ):
            await sync_plans_reply_keyboard(
                message, session, db_user, state
            )

        present.assert_awaited_once()
        message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
