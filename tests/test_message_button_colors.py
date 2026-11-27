"""The colour step of the bot's message buttons: each chosen button gets a web-form colour before sending."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot import message_button_picker as picker
from app.services.button_styles import STYLE_OPTIONS


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data)
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


class ColorChoiceTests(unittest.TestCase):
    def test_a_tap_walks_the_web_forms_colours_and_wraps(self) -> None:
        self.assertEqual(picker.STYLE_CYCLE, tuple(v for v, _l, _c in STYLE_OPTIONS))
        seen = [""]
        for _ in STYLE_OPTIONS:
            seen.append(picker.next_style(seen[-1]))
        self.assertEqual(seen, ["", "primary", "success", "danger", ""])
        self.assertEqual(picker.next_style("bogus"), "primary")

    def test_each_button_is_drawn_in_its_colour_with_send_and_back(self) -> None:
        labels = picker.option_labels(renew=False, labels=None, links={})
        markup = picker.colors_markup(["shop", "support"], {"shop": "success"}, labels=labels)
        rows = markup.inline_keyboard
        self.assertEqual([(b.callback_data, b.style) for b in (rows[0][0], rows[1][0])],
                         [("mbp:c:shop", "success"), ("mbp:c:support", None)])
        self.assertIn("سبز", rows[0][0].text)
        # «ارسال» on its own row, then the shared «بازگشت» alone on the last row, like every other screen.
        self.assertEqual([b.callback_data for b in rows[-2]], [picker.CB_SEND])
        self.assertEqual([b.callback_data for b in rows[-1]], [picker.CB_YES])
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for row in rows for b in row))


class ColorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = _state()
        message = MagicMock()
        message.answer = AsyncMock()
        await picker.ask_for_buttons(message, self.state, flow="colors", shop_id=None)
        await self.state.update_data(mbp_selected=["shop", "support"])
        self.finished = AsyncMock()
        picker.register_flow("colors", self.finished)
        # The colour step reads the sender's menu labels; these tests have no database.
        labels = patch.object(picker, "sender_screen_labels", AsyncMock(return_value={}))
        labels.start()
        self.addCleanup(labels.stop)
        self.session = MagicMock()

    async def test_continue_opens_the_colour_step_instead_of_sending(self) -> None:
        callback = _callback(picker.CB_DONE)
        await picker.on_done(callback, self.state, self.session)
        self.finished.assert_not_awaited()
        self.assertEqual(callback.message.edit_text.await_args.args[0], picker.COLORS)

    async def test_chosen_colours_are_sent_with_the_buttons(self) -> None:
        await picker.on_color(_callback("mbp:c:shop"), self.state, self.session)
        await picker.on_color(_callback("mbp:c:shop"), self.state, self.session)
        await picker.on_send(_callback(picker.CB_SEND), self.state, MagicMock(), MagicMock())
        buttons = self.finished.await_args.args[4]
        self.assertEqual([(b.target, b.style) for b in buttons], [("shop", "success"), ("support", "")])

    async def test_a_button_that_was_not_chosen_cannot_be_coloured(self) -> None:
        callback = _callback("mbp:c:wallet")
        await picker.on_color(callback, self.state, self.session)
        callback.answer.assert_awaited_with(picker.EXPIRED, show_alert=True)
        self.assertEqual((await self.state.get_data())["mbp_styles"], {})

    async def test_a_button_removed_on_the_checklist_drops_its_colour(self) -> None:
        await picker.on_color(_callback("mbp:c:support"), self.state, self.session)
        await self.state.update_data(mbp_selected=["shop"])
        await picker.on_send(_callback(picker.CB_SEND), self.state, MagicMock(), MagicMock())
        self.assertEqual([(b.target, b.style) for b in self.finished.await_args.args[4]], [("shop", "")])


if __name__ == "__main__":
    unittest.main()
