"""Ticket surfaces expose the opener's name/@username and numeric Telegram id."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.bot.handlers.admin import _ticket_thread_text
from app.bot.handlers.ticket_actions import _thread_preview
from app.services.formatting import bot_user_ticket_who


class TicketOpenerIdentityTests(unittest.TestCase):
    def test_panel_who_line_keeps_name_and_telegram_id(self) -> None:
        user = SimpleNamespace(full_name="Ali", username="ali_x", telegram_id=424242, id=7)
        self.assertEqual(bot_user_ticket_who(user), "Ali (@ali_x) · آیدی 424242")

    def test_synthetic_telegram_id_is_omitted_from_who_line(self) -> None:
        user = SimpleNamespace(full_name="Bot", username=None, telegram_id=-3, id=8)
        self.assertEqual(bot_user_ticket_who(user), "Bot")

    def test_admin_thread_includes_opener_identity(self) -> None:
        ticket = SimpleNamespace(id=3, subject="موضوع", messages=[])
        opener = SimpleNamespace(full_name="Sara", username="sara", telegram_id=555)
        text = _ticket_thread_text(ticket, opener)
        self.assertIn("<b>کاربر:</b> Sara", text)
        self.assertIn("<b>یوزرنیم:</b> @sara", text)
        self.assertIn("<b>آیدی:</b> 555", text)

    def test_staff_reply_preview_includes_opener_identity(self) -> None:
        ticket = SimpleNamespace(id=4, subject="سوال", messages=[])
        opener = SimpleNamespace(full_name=None, username="buyer", telegram_id=123456789)
        text = _thread_preview(ticket, as_staff=True, opener=opener)
        self.assertIn("<b>کاربر:</b> @buyer", text)
        self.assertIn("<b>آیدی:</b> 123456789", text)
        customer_text = _thread_preview(ticket, as_staff=False, opener=opener)
        self.assertNotIn("آیدی", customer_text)


if __name__ == "__main__":
    unittest.main()
