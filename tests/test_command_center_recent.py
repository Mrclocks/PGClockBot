"""Recent command center searches: kept per person, never a subscription link, and replayed safely."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import command_center
from app.services import bot_feedback_texts as feedback

LINK = "https://sub.example.com/sub/ShopTwoTokenAbcdef0123456789ABCDEF-_xyzq"


def _state() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))


class RememberTests(unittest.TestCase):
    def test_newest_first_without_repeats_and_bounded(self) -> None:
        recent: list[str] = []
        for query in ("کارت", "تست", "بکاپ", "کارت", "رسید", "پلن", "کاربر"):
            recent = command_center.remembered(recent, query)
        self.assertEqual(recent, ["کاربر", "پلن", "رسید", "کارت", "بکاپ"])

    def test_links_and_too_short_queries_are_not_kept(self) -> None:
        self.assertEqual(command_center.remembered(["کارت"], command_center.LINK_QUERY), ["کارت"])
        self.assertEqual(command_center.remembered(["کارت"], "ک"), ["کارت"])

    def test_panel_buttons_carry_an_index_not_the_query(self) -> None:
        rows = command_center.recent_rows({command_center.RECENT_KEY: ["کارت", "تست", "بکاپ"]})
        self.assertEqual([b.callback_data for row in rows for b in row], ["cmd:q:0", "cmd:q:1", "cmd:q:2"])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, data: str) -> MagicMock:
        callback = MagicMock()
        callback.data = data
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        return callback

    async def test_a_pasted_link_never_reaches_the_recent_list(self) -> None:
        state = _state()
        message = MagicMock()
        message.text = LINK
        viewer = command_center.CommandViewer([], "nv:adm:home")
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=viewer)), patch.object(
            command_center, "finish_text_step", AsyncMock()
        ), patch.object(command_center, "delete_user_message", AsyncMock()):
            await command_center.run_command_search(message, MagicMock(), SimpleNamespace(id=1), state)
        self.assertEqual((await state.get_data())[command_center.RECENT_KEY], [])

    async def test_a_recent_search_runs_again_and_a_stale_index_is_refused(self) -> None:
        state = _state()
        await state.update_data(**{command_center.RECENT_KEY: ["کارت"]})
        viewer = command_center.CommandViewer([command_center.CommandEntry("کارت", "پنل", "m:1", command_center.CommandKind.MENU)], "nv:adm:home")
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=viewer)), patch.object(
            command_center, "safe_edit_text", AsyncMock()
        ) as edit:
            await command_center.repeat_recent_search(self._callback("cmd:q:0"), MagicMock(), SimpleNamespace(id=1), state)
            stale = self._callback("cmd:q:3")
            await command_center.repeat_recent_search(stale, MagicMock(), SimpleNamespace(id=1), state)
        markup = edit.await_args_list[0].kwargs["reply_markup"]
        self.assertIn("m:1", [b.callback_data for row in markup.inline_keyboard for b in row])
        stale.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)
        self.assertEqual(edit.await_count, 1)

    async def test_someone_without_access_cannot_replay(self) -> None:
        state = _state()
        await state.update_data(**{command_center.RECENT_KEY: ["کارت"]})
        callback = self._callback("cmd:q:0")
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=None)):
            await command_center.repeat_recent_search(callback, MagicMock(), SimpleNamespace(id=1), state)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)


if __name__ == "__main__":
    unittest.main()
