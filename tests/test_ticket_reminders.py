"""Unanswered ticket reminders: which tickets are due, once per waiting period, to their own shop."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, Ticket, TicketMessage, TicketStatus
from app.jobs import ticket_reminders as job
from app.services import notifications
from app.services.ticket_reminders import (
    PLATFORM_KEY,
    claim_reminder,
    due_tickets,
    reminder_cutoffs,
    reminder_hours,
)
from app.services.users import clear_settings_cache, set_setting
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SHOP, OTHER_SHOP = 2, 3
SHOP_OLD, SHOP_ANSWERED, SHOP_FRESH, PLATFORM_OLD, OTHER_OLD, SHOP_CLOSED = 11, 12, 13, 14, 15, 16


class ReminderHoursTests(unittest.TestCase):
    def test_only_whole_hours_in_range_turn_reminders_on(self) -> None:
        self.assertEqual(reminder_hours("6"), 6)
        for raw in ("0", "", None, "-3", "169", "2.5", "abc"):
            with self.subTest(raw=raw):
                self.assertEqual(reminder_hours(raw), 0)


class TicketReminderCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all([
            BotUser(id=SHOP, telegram_id=1002, referral_code="s2", role="reseller"),
            BotUser(id=OTHER_SHOP, telegram_id=1003, referral_code="s3", role="reseller"),
            BotUser(id=4, telegram_id=1004, referral_code="c4", role="user", reseller_id=SHOP),
            BotUser(id=5, telegram_id=1005, referral_code="c5", role="user"),
            BotUser(id=6, telegram_id=1006, referral_code="c6", role="user", reseller_id=OTHER_SHOP),
        ])
        await self.session.flush()
        self.add_ticket(SHOP_OLD, user_id=4, shop=SHOP, hours_ago=10)
        self.add_ticket(SHOP_ANSWERED, user_id=4, shop=SHOP, hours_ago=10, status=TicketStatus.ANSWERED.value)
        self.add_ticket(SHOP_FRESH, user_id=4, shop=SHOP, hours_ago=1)
        self.add_ticket(PLATFORM_OLD, user_id=5, shop=None, hours_ago=30)
        self.add_ticket(OTHER_OLD, user_id=6, shop=OTHER_SHOP, hours_ago=30)
        self.add_ticket(SHOP_CLOSED, user_id=4, shop=SHOP, hours_ago=30, status=TicketStatus.CLOSED.value)
        await self.session.commit()
        await set_setting(self.session, "ticket_reminder_hours", "6", reseller_id=SHOP)

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        clear_settings_cache()

    def add_ticket(self, ticket_id: int, *, user_id: int, shop: int | None, hours_ago: int,
                   status: str = TicketStatus.OPEN.value) -> None:
        at = NOW - timedelta(hours=hours_ago)
        self.session.add(Ticket(id=ticket_id, user_id=user_id, reseller_id=shop, subject=f"<i>t{ticket_id}</i>",
                                status=status, created_at=at - timedelta(hours=1), updated_at=at))
        self.session.add(TicketMessage(ticket_id=ticket_id, sender_id=1, is_staff=False, body="x", created_at=at))

    async def due_ids(self) -> list[int]:
        return [d.ticket_id for d in await due_tickets(self.session, await reminder_cutoffs(self.session, now=NOW))]


class DueTicketTests(TicketReminderCase):
    async def test_only_open_tickets_past_their_own_shops_hours_are_due(self) -> None:
        self.assertEqual(await self.due_ids(), [SHOP_OLD])

    async def test_the_platform_and_other_shops_follow_their_own_switch(self) -> None:
        await set_setting(self.session, "ticket_reminder_hours", "24", reseller_id=None)
        self.assertEqual(await self.due_ids(), [PLATFORM_OLD, SHOP_OLD])
        cutoffs = await reminder_cutoffs(self.session, now=NOW)
        self.assertEqual(set(cutoffs), {PLATFORM_KEY, SHOP})

    async def test_a_waiting_period_is_reminded_once(self) -> None:
        [due] = await due_tickets(self.session, await reminder_cutoffs(self.session, now=NOW))
        before = (await self.session.get(Ticket, SHOP_OLD)).updated_at
        self.assertTrue(await claim_reminder(self.session, due, now=NOW))
        self.assertFalse(await claim_reminder(self.session, due, now=NOW), "a second worker loses the claim")
        self.assertEqual(await self.due_ids(), [])
        row = await self.session.get(Ticket, SHOP_OLD)
        await self.session.refresh(row)
        self.assertEqual(row.updated_at, before, "a reminder is not activity on the ticket")

    async def test_a_new_customer_message_starts_a_new_waiting_period(self) -> None:
        [due] = await due_tickets(self.session, await reminder_cutoffs(self.session, now=NOW))
        await claim_reminder(self.session, due, now=NOW - timedelta(hours=3))
        self.session.add(TicketMessage(ticket_id=SHOP_OLD, sender_id=1, is_staff=False, body="y",
                                       created_at=NOW - timedelta(hours=2)))
        await self.session.commit()
        self.assertEqual(await self.due_ids(), [], "the new message is not yet older than six hours")
        later = await reminder_cutoffs(self.session, now=NOW + timedelta(hours=5))
        self.assertEqual([d.ticket_id for d in await due_tickets(self.session, later)], [SHOP_OLD, SHOP_FRESH])

    async def test_an_answered_ticket_cannot_be_claimed(self) -> None:
        [due] = await due_tickets(self.session, await reminder_cutoffs(self.session, now=NOW))
        row = await self.session.get(Ticket, SHOP_OLD)
        row.status = TicketStatus.ANSWERED.value
        await self.session.commit()
        self.assertFalse(await claim_reminder(self.session, due, now=NOW))


class ReminderJobTests(TicketReminderCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await set_setting(self.session, "ticket_reminder_hours", "24", reseller_id=None)
        self.notify = AsyncMock(return_value=1)
        self.patches = [
            patch.object(job, "SessionLocal", self.Session),
            patch.object(job, "notify_ticket_reminder", self.notify),
            patch.object(job, "SEND_PAUSE_SECONDS", 0),
            patch.object(job, "datetime", wraps=datetime),
        ]
        for p in self.patches:
            p.start()
        job.datetime.now = lambda tz=None: NOW

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await super().asyncTearDown()

    async def test_each_due_ticket_is_sent_once_to_its_shop(self) -> None:
        self.assertEqual(await job.run_ticket_reminders(object()), 2)
        shops = {call.kwargs["ticket_id"]: call.kwargs["shop_id"] for call in self.notify.await_args_list}
        self.assertEqual(shops, {PLATFORM_OLD: None, SHOP_OLD: SHOP})
        self.assertEqual(await job.run_ticket_reminders(object()), 0, "a second run sends nothing")
        self.assertEqual(self.notify.await_count, 2)

    async def test_one_failing_ticket_does_not_stop_the_batch(self) -> None:
        self.notify.side_effect = [RuntimeError("telegram down"), 1]
        with self.assertLogs(job.logger, "ERROR"):
            self.assertEqual(await job.run_ticket_reminders(object()), 1)
        self.assertEqual(self.notify.await_count, 2)


class ReminderNoticeTests(TicketReminderCase):
    async def test_the_notice_goes_to_the_tickets_shop_and_escapes_the_subject(self) -> None:
        dispatch = AsyncMock(return_value=1)
        with patch.object(notifications, "_dispatch_dual_notify", dispatch):
            await notifications.notify_ticket_reminder(
                object(), self.session, ticket_id=SHOP_OLD, user_id=4, shop_id=SHOP,
                subject="<i>t11</i>", waiting_seconds=10 * 3600,
            )
        kwargs = dispatch.await_args.kwargs
        self.assertEqual((kwargs["ticket_reseller_id"], kwargs["ticket_user_id"]), (SHOP, 4))
        self.assertEqual(kwargs["replace_key"], f"ticket:{SHOP_OLD}")
        body = dispatch.await_args.args[3]
        self.assertIn("&lt;i&gt;t11&lt;/i&gt;", body)
        self.assertNotIn("<i>t11</i>", body)


class TicketReminderMigrationTests(unittest.TestCase):
    def test_upgrade_twice_then_downgrade_keeps_tickets(self) -> None:
        spec = importlib.util.spec_from_file_location("ticket_reminders_mig", ROOT / "alembic/versions/0141_ticket_reminders.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE tickets (id INTEGER PRIMARY KEY, subject TEXT)"))
            conn.execute(text("INSERT INTO tickets VALUES (1, 'ت')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIn("reminded_at", {c["name"] for c in sa.inspect(conn).get_columns("tickets")})
                migration.downgrade()
                self.assertNotIn("reminded_at", {c["name"] for c in sa.inspect(conn).get_columns("tickets")})
            self.assertEqual(conn.execute(text("SELECT subject FROM tickets")).scalar(), "ت")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
