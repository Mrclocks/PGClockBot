"""3.5.1 — HTML parse_mode on notify bots and outbound HTML messages."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.enums import ParseMode


class NotifyBotDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_notify_bot_uses_html_create_bot(self):
        from app.services.reseller_bots import open_notify_bot_for_user

        user = MagicMock()
        user.reseller_id = None
        fake = MagicMock()
        fake.default = MagicMock(parse_mode=ParseMode.HTML)

        with patch("app.bot.create_bot", return_value=fake) as create_bot, patch(
            "app.services.reseller_bots.get_reseller_bot_manager", return_value=None
        ):
            bot, should_close = await open_notify_bot_for_user(AsyncMock(), user)

        create_bot.assert_called_once_with()
        self.assertIs(bot, fake)
        self.assertTrue(should_close)
        self.assertEqual(bot.default.parse_mode, ParseMode.HTML)

    async def test_send_to_user_chat_passes_html_parse_mode(self):
        from app.db.models import BotUser
        from app.services.notifications import _send_to_user_chat

        user = BotUser(id=1, telegram_id=42, full_name="U", role="user", username="u")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        bot.token = "main-token"

        with patch(
            "app.bot.create_bot",
            return_value=bot,
        ), patch(
            "app.services.reseller_bots.open_notify_bot_for_reseller",
            new=AsyncMock(return_value=(None, False)),
        ):
            ok = await _send_to_user_chat(AsyncMock(), user, "<b>hi</b>")

        self.assertTrue(ok)
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs.get("parse_mode"), "HTML")


if __name__ == "__main__":
    unittest.main()
