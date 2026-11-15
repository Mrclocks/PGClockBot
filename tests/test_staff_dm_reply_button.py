"""Staff DMs to customers carry پاسخ / بستن like ticket notices."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import TicketStatus
from app.services.tickets import STAFF_OUTBOUND_SUBJECT, create_staff_outbound_ticket
from app.services.users_quick import send_staff_dm


class CreateStaffOutboundTicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_answered_staff_message_with_shop_scope(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        ticket = await create_staff_outbound_ticket(
            session,
            user_id=7,
            body="سلام، رسید را بررسی کردیم",
            sender_tg=9001,
            reseller_id=3,
        )

        self.assertEqual(ticket.user_id, 7)
        self.assertEqual(ticket.subject, STAFF_OUTBOUND_SUBJECT)
        self.assertEqual(ticket.status, TicketStatus.ANSWERED.value)
        self.assertEqual(ticket.reseller_id, 3)
        self.assertEqual(session.add.call_count, 2)
        msg = session.add.call_args_list[1].args[0]
        self.assertTrue(msg.is_staff)
        self.assertEqual(msg.sender_id, 9001)
        self.assertEqual(msg.body, "سلام، رسید را بررسی کردیم")
        session.commit.assert_awaited()

    async def test_platform_scope_keeps_reseller_id_none(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        ticket = await create_staff_outbound_ticket(
            session,
            user_id=1,
            body="hi",
            sender_tg=1,
            reseller_id=None,
        )
        self.assertIsNone(ticket.reseller_id)


class SendStaffDmReplyMarkupTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_staff_dm_attaches_ticket_reply_buttons(self) -> None:
        session = AsyncMock()
        user = SimpleNamespace(id=11, telegram_id=222, is_blocked=False, reseller_id=None)
        bot = AsyncMock()
        bot.session = AsyncMock()
        ticket = SimpleNamespace(id=55)

        with (
            patch(
                "app.services.tickets.create_staff_outbound_ticket",
                AsyncMock(return_value=ticket),
            ) as create_tkt,
            patch(
                "app.services.reseller_bots.open_notify_bot_for_user",
                AsyncMock(return_value=(bot, False)),
            ),
            patch(
                "app.services.notifications.ticket_action_markup",
                return_value="MARKUP",
            ) as markup,
        ):
            await send_staff_dm(
                session,
                user,
                "متن تست",
                shop_id=None,
                actor="Admin",
                sender_tg=999,
            )

        create_tkt.assert_awaited_once()
        kwargs = create_tkt.await_args.kwargs
        self.assertEqual(kwargs["user_id"], 11)
        self.assertEqual(kwargs["body"], "متن تست")
        self.assertEqual(kwargs["sender_tg"], 999)
        self.assertIsNone(kwargs["reseller_id"])
        markup.assert_called_once_with(55)
        bot.send_message.assert_awaited_once()
        send_kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(send_kwargs.get("reply_markup"), "MARKUP")
        self.assertEqual(send_kwargs.get("parse_mode"), "HTML")
        self.assertIn("پیام پشتیبانی", bot.send_message.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
