"""The Owner's customer search answers in place, like the shop search: the prompt becomes the
result and the menu keyboard is never swapped for a submenu."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from app.bot import chat_cleanup, nav_input
from app.bot.chat_cleanup import ThrowawayLog
from app.bot.handlers import admin
from app.db.models import BotUser
from tests.test_chat_cleanup import _Chat
from tests.test_chat_cleanup_forms import _State

BUTTONS = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:users")]])


class _CleanupOn(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(chat_cleanup, "_log", ThrowawayLog())
        patcher.start()
        self.addCleanup(patcher.stop)
        token = chat_cleanup._cleanup_on.set(True)
        self.addCleanup(chat_cleanup._cleanup_on.reset, token)


class AdminSearchTests(_CleanupOn):
    """The Owner's customer search behaves like the shop search: the prompt becomes the result."""

    async def asyncSetUp(self) -> None:
        self.chat = _Chat()
        self.prompt = self.chat.message()
        self.chat.bot.edit_message_text = AsyncMock()
        self.state = _State(
            current="AdminStates:user_search",
            data={nav_input.PROMPT_MSG_ID: self.prompt.message_id, nav_input.PROMPT_CHAT_ID: 100},
        )
        for target in ("app.bot.auth.require_bot_owner", "app.bot.handlers.admin._is_admin"):
            patcher = patch(target, AsyncMock(return_value=True) if "require" in target else MagicMock(return_value=True))
            patcher.start()
            self.addCleanup(patcher.stop)

    async def search(self, text: str, **patches) -> MagicMock:
        typed = self.chat.message(from_bot=False)
        typed.text = typed.html_text = text
        typed.delete = AsyncMock()
        with patch.multiple(admin, **patches):
            await admin.adm_users_search(typed, self.state, MagicMock(), MagicMock(role="admin"))
        return typed

    def assert_shown_in_place(self, typed: MagicMock, text: str) -> None:
        typed.answer.assert_not_awaited()  # no «نتیجه جستجو» bubble, no reply keyboard swap
        edit = self.chat.bot.edit_message_text.await_args.kwargs
        self.assertEqual((edit["message_id"], edit["text"]), (self.prompt.message_id, text))
        self.assertIsInstance(edit["reply_markup"], InlineKeyboardMarkup)
        self.assertIsNone(self.state.current)

    async def test_an_exact_match_turns_the_prompt_into_the_customer_card(self) -> None:
        user = BotUser(id=10, telegram_id=5010, full_name="Ali")
        typed = await self.search(
            "5010",
            find_bot_user=AsyncMock(return_value=user),
            _user_card=AsyncMock(return_value=("card", BUTTONS)),
        )
        self.assert_shown_in_place(typed, "card")
        self.assertIn(typed.message_id, self.chat.deleted())

    async def test_a_partial_match_shows_the_result_list_in_place(self) -> None:
        typed = await self.search(
            "ali",
            find_bot_user=AsyncMock(return_value=None),
            staff_user_search=AsyncMock(return_value=("list", BUTTONS)),
        )
        self.assert_shown_in_place(typed, "list")

    async def test_a_subscription_link_shows_its_owner_in_place_and_the_link_is_deleted(self) -> None:
        typed = await self.search(
            "https://sub.example/sub/abcdefghijklmnop1234",
            link_token=MagicMock(return_value="abcdefghijklmnop1234"),
            staff_lookup=AsyncMock(return_value=("owner", BUTTONS)),
        )
        self.assert_shown_in_place(typed, "owner")
        typed.delete.assert_awaited()

    async def test_no_search_path_sends_a_reply_keyboard(self) -> None:
        typed = await self.search(
            "ali",
            find_bot_user=AsyncMock(return_value=None),
            staff_user_search=AsyncMock(return_value=("list", BUTTONS)),
        )
        markups = [c.kwargs.get("reply_markup") for c in typed.answer.await_args_list]
        markups += [c.kwargs.get("reply_markup") for c in self.chat.bot.edit_message_text.await_args_list]
        self.assertFalse(any(isinstance(m, ReplyKeyboardMarkup) for m in markups))


if __name__ == "__main__":
    unittest.main()
