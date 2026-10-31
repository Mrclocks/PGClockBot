"""Each ticket keeps one staff notice per chat, and closing the ticket removes it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db.models import BotUser, Ticket, TicketStatus
from app.services import notifications, ticket_notices
from app.services.tickets import close_ticket
from app.services.ticket_notices import TICKET_CLOSED_TEXT, ticket_notice_key
from tests.test_replaceable_notices import NoticeCase, fake_bot


class TicketNoticeTests(NoticeCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=7001, referral_code="c1"))
            await session.flush()
            session.add_all([Ticket(id=5, user_id=1, subject="s", status=TicketStatus.OPEN.value),
                             Ticket(id=6, user_id=1, subject="t", status=TicketStatus.OPEN.value)])
            await session.commit()
        self.bot = fake_bot()
        self.next_id = iter(range(100, 200))

    async def notify(self, ticket_id: int, *, first: bool) -> None:
        async def send_to_chats(send_bot, targets, text, *, markup=None, photo=None, sent_messages=None):
            for chat_id in targets:
                sent_messages.append(SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=next(self.next_id)))
            return len(targets)

        with patch.object(notifications, "_send_to_chats", send_to_chats), \
                patch.object(notifications, "notify_enabled", AsyncMock(return_value=True)), \
                patch.object(notifications, "get_settings", return_value=SimpleNamespace(admin_ids=[501, 502])):
            async with self.Session() as session:
                if first:
                    await notifications.notify_new_ticket(self.bot, session, ticket_id=ticket_id, subject="s",
                                                          user_name="u", ticket_user_id=1, ticket_reseller_id=None)
                else:
                    await notifications.notify_ticket_message(self.bot, session, ticket_id=ticket_id, subject="s",
                                                              body="b", from_staff=False, ticket_user_id=1,
                                                              ticket_reseller_id=None)

    async def test_a_new_customer_message_replaces_the_tickets_previous_notice(self) -> None:
        await self.notify(5, first=True)
        await self.notify(6, first=True)
        await self.notify(5, first=False)
        deleted = sorted((c.kwargs["chat_id"], c.kwargs["message_id"]) for c in self.bot.delete_message.await_args_list)
        self.assertEqual(deleted, [(501, 100), (502, 101)])
        keys = sorted(row[3] for row in await self.stored())
        self.assertEqual(keys, [ticket_notice_key(5)] * 2 + [ticket_notice_key(6)] * 2)

    async def test_closing_a_ticket_removes_only_its_notices(self) -> None:
        await self.notify(5, first=True)
        await self.notify(6, first=True)
        self.bot.delete_message.reset_mock()
        with patch.object(ticket_notices, "_notice_bot", AsyncMock(return_value=(self.bot, False))):
            async with self.Session() as session:
                await close_ticket(session, await session.get(Ticket, 5))
        deleted = sorted((c.kwargs["chat_id"], c.kwargs["message_id"]) for c in self.bot.delete_message.await_args_list)
        self.assertEqual(deleted, [(501, 100), (502, 101)])
        self.assertEqual(sorted(row[3] for row in await self.stored()), [ticket_notice_key(6)] * 2)

    async def test_a_notice_too_old_to_delete_says_the_ticket_is_closed(self) -> None:
        from datetime import datetime, timedelta, timezone

        from app.db.models import BotChatMessage
        from app.services.replaceable_notices import KIND_NOTICE

        async with self.Session() as session:
            session.add(BotChatMessage(bot_id=1, chat_id=501, message_id=9, kind=KIND_NOTICE,
                                       sent_at=datetime.now(timezone.utc) - timedelta(days=3),
                                       replace_key=ticket_notice_key(5)))
            await session.commit()
        with patch.object(ticket_notices, "_notice_bot", AsyncMock(return_value=(self.bot, False))):
            async with self.Session() as session:
                await close_ticket(session, await session.get(Ticket, 5))
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["text"], TICKET_CLOSED_TEXT)

    async def test_without_a_bot_closing_still_works(self) -> None:
        with patch.object(ticket_notices, "_notice_bot", AsyncMock(return_value=(None, False))):
            async with self.Session() as session:
                ticket = await close_ticket(session, await session.get(Ticket, 5))
        self.assertEqual(ticket.status, TicketStatus.CLOSED.value)

    async def test_a_temporary_bot_is_closed_after_use(self) -> None:
        temporary = fake_bot()
        temporary.session = SimpleNamespace(close=AsyncMock())
        with patch.object(ticket_notices, "_notice_bot", AsyncMock(return_value=(temporary, True))):
            async with self.Session() as session:
                await close_ticket(session, await session.get(Ticket, 5))
        temporary.session.close.assert_awaited_once()


class NoticeBotTests(NoticeCase):
    async def test_a_shop_ticket_is_tidied_through_that_shops_bot_only(self) -> None:
        shop_bot = fake_bot(2)
        opener = AsyncMock(return_value=(shop_bot, False))
        with patch.object(ticket_notices, "open_notify_bot_for_reseller", opener):
            async with self.Session() as session:
                self.assertEqual(await ticket_notices._notice_bot(session, 42), (shop_bot, False))
        self.assertEqual(opener.await_args.args[1], 42)

    async def test_a_platform_ticket_without_a_bot_token_is_skipped(self) -> None:
        with patch.object(ticket_notices, "get_settings", return_value=SimpleNamespace(bot_token="")):
            async with self.Session() as session:
                self.assertEqual(await ticket_notices._notice_bot(session, None), (None, False))
