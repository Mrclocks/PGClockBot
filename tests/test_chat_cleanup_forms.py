"""Forms and one-line notices leave no clutter; records sent meanwhile stay."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.state import State
from aiogram.methods import SendMessage
from aiogram.types import ReplyKeyboardMarkup

from app.bot import chat_cleanup, chat_screen, nav_inline, nav_input
from app.bot.chat_cleanup import ThrowawayLog
from app.services import bot_feedback_texts as feedback
from app.services.chat_records import keep_sent
from tests.test_chat_cleanup import PANEL, _Chat

MENU = ReplyKeyboardMarkup(keyboard=[])


class _CleanupOn(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(chat_cleanup, "_log", ThrowawayLog())
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


class ShortNoticeTests(_CleanupOn):
    """A one-line notice is an ordinary temporary message: the next screen deletes it."""

    async def _send(self, chat: _Chat, text: str, *, chat_id: int = 100, keep: bool = False) -> int:
        sent = chat.message()
        method = SendMessage(chat_id=chat_id, text=text)
        with chat_screen.turn((1, 100)), patch.object(chat_screen, "Message", MagicMock):
            if keep:
                with keep_sent():
                    await chat_screen.screen_requests(AsyncMock(return_value=sent), chat.bot, method)
            else:
                await chat_screen.screen_requests(AsyncMock(return_value=sent), chat.bot, method)
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

    async def test_records_and_messages_to_other_chats_stay(self) -> None:
        chat = _Chat()
        await self._send(chat, "سرویس شما آماده است: vless://…", keep=True)
        await self._send(chat, "سفارش جدید", chat_id=200)
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="hub", inline=PANEL)
        chat.bot.delete_messages.assert_not_awaited()
