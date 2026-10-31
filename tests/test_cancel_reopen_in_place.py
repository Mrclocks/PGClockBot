"""Inline cancel reopens the previous panel in the same message, without chrome bubbles."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import staff_cancel
from app.bot.handlers import admin_plans


def _panel_message(*, from_bot: bool) -> MagicMock:
    message = MagicMock()
    message.message_id = 10
    message.chat = SimpleNamespace(id=4)
    message.from_user = SimpleNamespace(is_bot=from_bot)
    message.answer = AsyncMock(return_value=None)
    message.edit_text = AsyncMock(return_value=True)
    message.bot.edit_message_text = AsyncMock()
    return message


async def _state(**data) -> FSMContext:
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=4, user_id=4))
    await state.update_data(**data)
    return state


class CancelReopenTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_adjust_cancel_edits_panel_without_heal_bubble(self):
        message = _panel_message(from_bot=True)
        state = await _state(capadj_uid=7, **{"capadj:7": {"days": 3, "gb": 5}})
        with patch("app.bot.nav_chrome.heal_main_reply", AsyncMock()) as heal:
            await staff_cancel.reopen_reseller_cap_adjust(
                message, MagicMock(), MagicMock(), state
            )
        heal.assert_not_awaited()
        message.answer.assert_not_awaited()
        message.edit_text.assert_awaited_once()
        self.assertIn("#7", message.edit_text.await_args.args[0])

    async def test_typed_capacity_cancel_shows_only_the_adjuster(self):
        message = _panel_message(from_bot=False)
        state = await _state(capadj_uid=7)
        with patch("app.bot.nav_chrome.heal_main_reply", AsyncMock()) as heal:
            await staff_cancel.reopen_reseller_cap_adjust(
                message, MagicMock(), MagicMock(), state
            )
        heal.assert_not_awaited()  # the menu keyboard was never replaced
        message.answer.assert_awaited_once()
        self.assertIn("تغییر ظرفیت نماینده #7", message.answer.await_args.args[0])

    async def test_user_message_cancel_edits_instead_of_sending(self):
        message = _panel_message(from_bot=True)
        state = await _state()
        await staff_cancel.reopen_reseller_user_msg(message, MagicMock(), MagicMock(), state)
        message.answer.assert_not_awaited()
        message.edit_text.assert_awaited_once()

    async def test_plans_cancel_from_inline_uses_given_user_and_skips_heal(self):
        message = _panel_message(from_bot=True)
        state = await _state()
        db_user = SimpleNamespace(id=1)
        sync = AsyncMock()
        with patch("app.bot.nav_chrome.heal_main_reply", AsyncMock()) as heal, patch.object(
            admin_plans, "sync_plans_reply_keyboard", sync
        ):
            await admin_plans._answer_plans_cancel(message, state, MagicMock(), db_user)
        heal.assert_not_awaited()
        message.answer.assert_not_awaited()
        sync.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
