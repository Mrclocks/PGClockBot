"""Finishing or cancelling a settings text input edits the prompt bubble."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import admin_settings, reseller_settings
from app.bot.nav_input import PROMPT_CHAT_ID, PROMPT_MSG_ID

CHAT_ID = 77
PROMPT_ID = 501


async def _prompted_state() -> FSMContext:
    state = FSMContext(
        MemoryStorage(), StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=CHAT_ID)
    )
    await state.update_data(**{PROMPT_MSG_ID: PROMPT_ID, PROMPT_CHAT_ID: CHAT_ID})
    return state


def _message(text: str = "hello") -> MagicMock:
    message = MagicMock()
    message.text = text
    message.chat = SimpleNamespace(id=CHAT_ID)
    message.bot.edit_message_text = AsyncMock()
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    return message


class AdminSettingsInPlaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_edits_prompt_instead_of_sending_new_messages(self):
        state = await _prompted_state()
        message = _message()
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            await admin_settings._finish_settings_text_edit(
                message, state, MagicMock(), MagicMock(), loc=("shop", "titles")
            )
        message.answer.assert_not_awaited()
        edit = message.bot.edit_message_text
        edit.assert_awaited_once()
        self.assertEqual(edit.await_args.kwargs["message_id"], PROMPT_ID)
        self.assertIn("✅ ذخیره شد.", edit.await_args.kwargs["text"])
        message.delete.assert_awaited_once()
        self.assertEqual(await state.get_state(), None)

    async def test_cancel_returns_hub_in_same_bubble(self):
        state = await _prompted_state()
        message = _message("انصراف")
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            await admin_settings._silent_settings_cancel(
                message, state, MagicMock(), MagicMock()
            )
        message.answer.assert_not_awaited()
        edit = message.bot.edit_message_text
        self.assertEqual(edit.await_args.kwargs["message_id"], PROMPT_ID)
        self.assertEqual(
            edit.await_args.kwargs["text"],
            "⚙️ <b>تنظیمات سریع</b>\n"
            "یک بخش را از دکمه‌های زیر انتخاب کنید.\n"
            "ظاهر، رنگ دکمه‌ها و گزارش روزانه را از وب‌پنل تنظیم کنید.",
        )

    async def test_save_sends_one_panel_when_prompt_is_unknown(self):
        state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=CHAT_ID)
        )
        message = _message()
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            await admin_settings._finish_settings_text_edit(
                message, state, MagicMock(), MagicMock(), loc=None
            )
        message.answer.assert_awaited_once()

    async def test_every_section_panel_builds_for_in_place_editing(self):
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            for sec_id, sec in admin_settings.SECTIONS.items():
                if sec.get("kind") in {"supports", "notify"}:
                    continue
                panel = await admin_settings._settings_location_panel(
                    MagicMock(), (sec_id, "")
                )
                self.assertIsNotNone(panel, sec_id)
                for sub in sec.get("subs") or []:
                    panel = await admin_settings._settings_location_panel(
                        MagicMock(), (sec_id, sub[0])
                    )
                    self.assertIsNotNone(panel, (sec_id, sub[0]))


class ResellerSettingsInPlaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_input_edits_prompt_and_deletes_reply(self):
        state = await _prompted_state()
        message = _message()
        profile = SimpleNamespace(bot_username="shopbot", user_id=5)
        with (
            patch(
                "app.services.resellers.get_reseller_panel_base_url",
                AsyncMock(return_value=""),
            ),
            patch.object(reseller_settings, "get_all_settings", AsyncMock(return_value={})),
        ):
            await reseller_settings._close_input_to_hub(
                message, state, MagicMock(), profile, note="✅ ذخیره شد."
            )
        message.answer.assert_not_awaited()
        edit = message.bot.edit_message_text
        self.assertEqual(edit.await_args.kwargs["message_id"], PROMPT_ID)
        self.assertIn("@shopbot", edit.await_args.kwargs["text"])
        message.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
