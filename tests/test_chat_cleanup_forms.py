"""Forms and one-line notices leave no clutter, and a typed reply that ends a form unfolds the menu keyboard again."""

from __future__ import annotations

import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.state import State
from aiogram.methods import SendMessage
from aiogram.types import ReplyKeyboardMarkup

from app.bot import chat_cleanup, menu_keyboard_reopen, nav_inline, nav_input
from app.bot.chat_cleanup import ThrowawayLog
from app.bot.menu_keyboard_reopen import MENU_REOPEN_TEXT, MenuKeyboardReopenMiddleware
from app.bot.reply_keyboard_state import KEYBOARD_FOLDED
from app.services import bot_feedback_texts as feedback
from tests.test_chat_cleanup import PANEL, _Chat

MENU = ReplyKeyboardMarkup(keyboard=[])


class _CleanupOn(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for patcher in (
            patch.object(chat_cleanup, "_log", ThrowawayLog()),
            patch.object(menu_keyboard_reopen, "_spare_menus", OrderedDict()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        token = chat_cleanup._cleanup_on.set(True)
        self.addCleanup(chat_cleanup._cleanup_on.reset, token)


class _State:
    def __init__(self, current: str | None = None, data: dict | None = None) -> None:
        self.current, self.data = current, dict(data or {})

    async def get_state(self) -> str | None:
        return self.current

    async def set_state(self, value) -> None:
        self.current = value.state if isinstance(value, State) else value

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, data: dict | None = None, **kw) -> None:
        self.data.update(data or {}, **kw)


class FormTests(_CleanupOn):
    async def test_the_typed_reply_goes_once_the_result_is_shown(self) -> None:
        chat = _Chat()
        prompt = chat.message()
        state = _State(data={nav_input.PROMPT_MSG_ID: prompt.message_id, nav_input.PROMPT_CHAT_ID: 100})
        typed = chat.message(from_bot=False)
        chat.bot.edit_message_text = AsyncMock()
        await nav_input.finish_text_step(typed, state, text="✅", inline=PANEL)
        self.assertEqual(chat.deleted(), [typed.message_id])

    async def test_a_ticket_the_person_wrote_stays(self) -> None:
        chat = _Chat()
        prompt = chat.message()
        state = _State(data={nav_input.PROMPT_MSG_ID: prompt.message_id, nav_input.PROMPT_CHAT_ID: 100})
        chat.bot.edit_message_text = AsyncMock()
        await nav_input.finish_text_step(chat.message(from_bot=False), state, text="✅", inline=PANEL, keep_input=True)
        self.assertEqual(chat.deleted(), [])

    async def test_a_next_step_prompt_clears_the_typed_reply_and_stays_itself(self) -> None:
        chat = _Chat()
        typed = chat.message(from_bot=False)
        with patch.dict(nav_input.CANCEL_REGISTRY, {"t": MagicMock()}):
            prompt = await nav_input.ask_text(typed, _State(), prompt="next?", cancel_code="t", fsm_state=State("s"))
            await nav_input.ask_text(chat.message(from_bot=False), _State(), prompt="again?", cancel_code="t", fsm_state=State("s"))
        self.assertIn(typed.message_id, chat.deleted())
        self.assertIn(prompt.message_id, chat.deleted())

    async def test_switched_off_nothing_is_deleted(self) -> None:
        chat_cleanup._cleanup_on.set(False)
        chat = _Chat()
        state = _State()
        await nav_input.finish_text_step(chat.message(from_bot=False), state, text="✅", inline=PANEL)
        chat.bot.delete_messages.assert_not_awaited()


class MenuReopenTests(_CleanupOn):
    async def _type(self, chat: _Chat, state: _State, handler) -> MagicMock:
        typed = chat.message(from_bot=False)
        typed.text, typed.entities = "my answer", None
        data = {"state": state, "session": MagicMock(), "db_user": SimpleNamespace(id=1)}
        with (
            patch.object(menu_keyboard_reopen, "get_all_settings", AsyncMock(return_value={})),
            patch.object(menu_keyboard_reopen, "build_main_reply_keyboard", AsyncMock(return_value=(MENU, {}, "user"))),
            patch.object(menu_keyboard_reopen, "Message", MagicMock),
        ):
            await MenuKeyboardReopenMiddleware()(handler, typed, data)
        return typed

    @staticmethod
    async def _ends_form(_event, data) -> None:
        await data["state"].set_state(None)

    def _menus(self, typed: MagicMock) -> list:
        return [c for c in typed.answer.await_args_list if c.args and c.args[0] == MENU_REOPEN_TEXT]

    async def test_a_form_ended_by_typing_brings_the_menu_back(self) -> None:
        chat = _Chat()
        state = _State("form:step", {KEYBOARD_FOLDED: True})
        typed = await self._type(chat, state, self._ends_form)
        self.assertIs(self._menus(typed)[0].kwargs["reply_markup"], MENU)
        self.assertFalse(state.data[KEYBOARD_FOLDED])

    async def test_the_previous_menu_message_is_replaced_not_piled_up(self) -> None:
        chat = _Chat()
        first = await self._type(chat, _State("form:step", {KEYBOARD_FOLDED: True}), self._ends_form)
        first_menu_id = first.message_id + 1  # the next id in this chat
        await self._type(chat, _State("form:step", {KEYBOARD_FOLDED: True}), self._ends_form)
        self.assertEqual(chat.deleted(), [first_menu_id])

    async def test_nothing_is_sent_while_the_form_continues_or_the_keyboard_is_open(self) -> None:
        chat = _Chat()
        still_typing = await self._type(chat, _State("form:step", {KEYBOARD_FOLDED: True}), AsyncMock())
        not_folded = await self._type(chat, _State("form:step"), self._ends_form)
        outside_form = await self._type(chat, _State(None, {KEYBOARD_FOLDED: True}), AsyncMock())
        self.assertEqual([self._menus(m) for m in (still_typing, not_folded, outside_form)], [[], [], []])

    async def test_no_second_menu_when_the_handler_already_sent_one(self) -> None:
        chat = _Chat()

        async def shows_home(_event, data) -> None:
            await data["state"].set_state(None)
            method = SendMessage(chat_id=100, text="home", reply_markup=MENU)
            await menu_keyboard_reopen._watch_menu_keyboards(AsyncMock(), chat.bot, method)

        typed = await self._type(chat, _State("form:step", {KEYBOARD_FOLDED: True}), shows_home)
        self.assertEqual(self._menus(typed), [])


if __name__ == "__main__":
    unittest.main()


class ShortNoticeTests(_CleanupOn):
    async def _send(self, chat: _Chat, text: str, markup=None) -> int:
        sent = chat.message()
        method = SendMessage(chat_id=100, text=text, reply_markup=markup)
        with patch.object(chat_cleanup, "Message", MagicMock):
            await chat_cleanup._note_throwaway_requests(AsyncMock(return_value=sent), chat.bot, method)
        return sent.message_id

    async def test_a_fixed_notice_goes_with_the_next_panel(self) -> None:
        chat = _Chat()
        cancelled = await self._send(chat, feedback.CANCELLED)
        waiting = await self._send(chat, "⏳")
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="hub", inline=PANEL)
        self.assertEqual(chat.deleted(), [cancelled, waiting])

    async def test_refusals_and_not_found_notices_go_with_the_next_panel(self) -> None:
        chat = _Chat()
        notices = [await self._send(chat, text) for text in (
            feedback.NO_ACCESS_REPLY, feedback.ACTION_NOT_ALLOWED_REPLY, feedback.TICKET_ALREADY_CLOSED,
            feedback.NO_PENDING_RECEIPT,
        )]
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="hub", inline=PANEL)
        self.assertEqual(chat.deleted(), notices)

    async def test_other_messages_and_the_menu_keyboard_stay(self) -> None:
        chat = _Chat()
        await self._send(chat, "سرویس شما آماده است: vless://…")
        await self._send(chat, feedback.CANCELLED, MENU)
        await self._send(chat, feedback.SENT)
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="hub", inline=PANEL)
        chat.bot.delete_messages.assert_not_awaited()
