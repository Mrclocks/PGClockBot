"""A flooded tap gets a toast and flooded text is dropped; the chat never gets a notice message."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.middlewares import RATE_LIMIT_TOAST, RateLimitMiddleware

PERSON = User(id=7, is_bot=False, first_name="a")


def _tap_update() -> Update:
    message = Message(message_id=5, date=datetime.now(timezone.utc), chat=Chat(id=7, type="private"))
    return Update(update_id=1, callback_query=CallbackQuery(
        id="1", from_user=PERSON, chat_instance="c", data="x", message=message))


def _text_update() -> Update:
    return Update(update_id=2, message=Message(
        message_id=6, date=datetime.now(timezone.utc), chat=Chat(id=7, type="private"), from_user=PERSON, text="hi"))


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def run_limited(self, update: Update) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
        handler, toast, reply = AsyncMock(), AsyncMock(), AsyncMock()
        with patch("app.bot.middlewares._rate_limited", return_value=True), \
                patch.object(CallbackQuery, "answer", toast), patch.object(Message, "answer", reply):
            self.assertIsNone(await RateLimitMiddleware()(handler, update, {}))
        return handler, toast, reply

    async def test_a_flooded_tap_gets_a_toast_not_a_message(self) -> None:
        handler, toast, reply = await self.run_limited(_tap_update())
        handler.assert_not_awaited()
        self.assertEqual(toast.await_args.args[0], RATE_LIMIT_TOAST)
        reply.assert_not_awaited()

    async def test_flooded_text_is_dropped_quietly(self) -> None:
        handler, toast, reply = await self.run_limited(_text_update())
        handler.assert_not_awaited()
        toast.assert_not_awaited()
        reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
