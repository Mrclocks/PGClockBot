"""A menu tap is told apart from typed text, and «منوی اصلی» on a panel just turns it into home."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, MessageEntity, User

from app.bot.reply_keyboard_state import is_menu_tap
from app.services.users import DEFAULT_SETTINGS

UI = dict(DEFAULT_SETTINGS)
WALLET_LABEL = UI["btn_wallet"]


def _message(text: str, *, premium: bool = False) -> Message:
    entities = [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="1")] if premium else None
    return Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=7, type="private"),
        from_user=User(id=7, is_bot=False, first_name="u"), text=text, entities=entities,
    )


class MenuTapTests(unittest.TestCase):
    def test_a_menu_tap_is_told_apart_from_typed_text(self) -> None:
        self.assertTrue(is_menu_tap(_message(WALLET_LABEL), UI))
        self.assertFalse(is_menu_tap(_message("💳 شارژ سریع"), UI))
        self.assertFalse(is_menu_tap(_message(WALLET_LABEL, premium=True), UI))


class HomeButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_home_button_edits_the_panel_into_home_and_sends_nothing_extra(self) -> None:
        from app.bot.handlers import start

        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))
        callback = MagicMock()
        callback.answer = AsyncMock()
        with patch.object(start, "render_home", AsyncMock()) as render:
            await start.cb_home(callback, MagicMock(), MagicMock(), state)
        self.assertTrue(render.await_args.kwargs["edit"])
        callback.message.answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
