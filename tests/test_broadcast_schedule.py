"""Scheduled group messages: the chosen Tehran time, waiting in the queue, and recounting when sending starts."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from sqlalchemy import select, update

from app.db.models import BotUser, BroadcastLog
from app.services.broadcast_queue import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_QUEUED,
    BroadcastRequest,
    active_broadcast_jobs,
    cancel_broadcast,
    queue_broadcast,
)
from app.services.broadcast_schedule import (
    PAST_TIME,
    TOO_FAR,
    checked_send_after,
    day_choices,
    schedule_label,
    send_time_from_form,
)
from app.services.shop_broadcast import new_shop_request_key, queue_shop_broadcast
from tests.test_broadcast_failures import SHOP_A, FailureCase
from tests.test_broadcast_pages import OWNER, SHOP_STAFF, PageCase

# 08:00 UTC on 24 Nov 2026 is 11:30 in Tehran (UTC+03:30, no daylight saving).
NOW = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)


class SendTimeTests(unittest.TestCase):
    def test_sending_now_is_the_default(self):
        self.assertIsNone(send_time_from_form({"send_day": "1", "send_time": "09:00"}, now=NOW))

    def test_a_day_and_time_are_read_in_tehran(self):
        form = {"schedule": "1", "send_day": "1", "send_time": "09:00"}
        self.assertEqual(send_time_from_form(form, now=NOW), datetime(2026, 11, 25, 5, 30, tzinfo=timezone.utc))
        today = {"schedule": "1", "send_day": "0", "send_time": "23:59"}
        self.assertEqual(send_time_from_form(today, now=NOW), datetime(2026, 11, 24, 20, 29, tzinfo=timezone.utc))

    def test_a_past_far_or_malformed_choice_is_refused(self):
        cases = (
            ({"send_day": "0", "send_time": "11:00"}, PAST_TIME),
            ({"send_day": "8", "send_time": "09:00"}, "روز"),
            ({"send_day": "-1", "send_time": "09:00"}, "روز"),
            ({"send_day": "1", "send_time": "24:00"}, "ساعت"),
            ({"send_day": "1", "send_time": "9am"}, "ساعت"),
        )
        for form, message in cases:
            with self.subTest(form=form), self.assertRaisesRegex(ValueError, message):
                send_time_from_form({"schedule": "1", **form}, now=NOW)
        with self.assertRaisesRegex(ValueError, TOO_FAR):
            checked_send_after(NOW + timedelta(days=9), now=NOW)
        with self.assertRaises(ValueError):
            checked_send_after(datetime(2026, 11, 25, 9, 0), now=NOW)

    def test_days_and_times_are_shown_in_persian(self):
        choices = day_choices(NOW)
        self.assertEqual([c.offset for c in choices], list(range(8)))
        self.assertTrue(choices[0].label.startswith("امروز · ۳ آذر"))
        self.assertTrue(choices[1].label.startswith("فردا"))
        self.assertEqual(schedule_label(datetime(2026, 11, 25, 5, 30, tzinfo=timezone.utc)), "۴ آذر، ساعت ۰۹:۰۰")


class ScheduledQueueTests(FailureCase):
    async def queue(self, send_after: datetime) -> BroadcastLog:
        return await queue_broadcast(
            self.session, text="later", audience="users", created_by="owner",
            request=BroadcastRequest(send_after=send_after),
        )

    async def make_due(self, log_id: int) -> None:
        await self.session.execute(
            update(BroadcastLog).where(BroadcastLog.id == log_id).values(send_after=datetime.now(timezone.utc))
        )
        await self.session.commit()

    async def test_a_scheduled_broadcast_waits_until_its_time(self):
        send_after = datetime.now(timezone.utc) + timedelta(hours=3)
        log = await self.queue(send_after)
        self.assertEqual(log.send_after, send_after)
        self.assertEqual(json.loads(log.audience_spec)["as_of"], send_after.isoformat())
        self.assertEqual(await active_broadcast_jobs(self.session), [])
        await self.run_all(log.id)
        self.assertEqual(self.bot.sent, [])
        await self.session.refresh(log)
        self.assertEqual(log.status, STATUS_QUEUED)
        await self.make_due(log.id)
        await self.run_all(log.id)
        await self.session.refresh(log)
        self.assertEqual((log.status, len(self.bot.sent)), (STATUS_DONE, log.total))

    async def test_the_audience_is_counted_again_when_sending_starts(self):
        log = await self.queue(datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(log.total, 6)
        self.session.add(BotUser(id=50, telegram_id=5050, referral_code="late", role="user"))
        await self.session.commit()
        await self.make_due(log.id)
        await self.run_all(log.id)
        await self.session.refresh(log)
        self.assertEqual((log.total, log.ok_count), (7, 7))
        self.assertIn(5050, self.bot.sent)

    async def test_a_scheduled_broadcast_can_be_cancelled_before_it_starts(self):
        log = await self.queue(datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertTrue(await cancel_broadcast(self.session, log.id, shop_id=None))
        await self.make_due(log.id)
        await self.run_all(log.id)
        await self.session.refresh(log)
        self.assertEqual((log.status, self.bot.sent), (STATUS_CANCELLED, []))

    async def test_a_past_time_is_refused_by_the_service_for_platform_and_shop(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        with self.assertRaisesRegex(ValueError, PAST_TIME):
            await self.queue(past)
        with self.assertRaisesRegex(ValueError, PAST_TIME):
            await queue_shop_broadcast(self.session, shop_id=SHOP_A, text="x", created_by="a",
                                       request_key=new_shop_request_key(), send_after=past)
        self.assertEqual(list(await self.session.scalars(select(BroadcastLog))), [])

    async def test_a_scheduled_shop_broadcast_sends_only_to_its_customers_when_due(self):
        log = await queue_shop_broadcast(
            self.session, shop_id=SHOP_A, text="x", created_by="a", request_key=new_shop_request_key(),
            send_after=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        await self.run_all(log.id)
        self.assertEqual(self.bot.sent, [])
        await self.make_due(log.id)
        await self.run_all(log.id)
        self.assertEqual(sorted(self.bot.sent), [2001, 2002])


class SchedulePageTests(PageCase):
    async def test_the_form_schedules_and_the_history_says_when(self):
        form = {"segment": "all", "text": "سلام", "request_token": "d" * 32, "schedule": "1",
                "send_day": "1", "send_time": "09:00"}
        response = await self.call(SHOP_STAFF, "POST", "/shop-broadcast", data=form)
        self.assertIn("زمان‌بندی شد", unquote(response.headers["location"]))
        row = await self.session.scalar(select(BroadcastLog))
        self.assertIsNotNone(row.send_after)
        html = (await self.call(SHOP_STAFF, "GET", "/shop-broadcast")).text
        self.assertIn("زمان‌بندی‌شده برای", html)
        self.assertIn('name="send_time"', html)

    async def test_a_past_time_on_the_form_is_a_safe_error_and_nothing_is_queued(self):
        form = {"segment": "all", "text": "x", "request_token": "e" * 32, "schedule": "1",
                "send_day": "0", "send_time": "00:00"}
        response = await self.call(OWNER, "POST", "/broadcast", data=form, platform=True)
        self.assertIn("err=", response.headers["location"])
        self.assertEqual(list(await self.session.scalars(select(BroadcastLog))), [])


class BroadcastScheduleMigrationTests(unittest.TestCase):
    def test_upgrade_twice_then_downgrade_keeps_broadcasts(self):
        import importlib.util
        from pathlib import Path

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import text

        from tests.postgres_support import scoped_sync_engine

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0135_broadcast_schedule.py"
        spec = importlib.util.spec_from_file_location("m0135", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, text TEXT)"))
            conn.execute(text("INSERT INTO broadcast_logs VALUES (1, 'kept')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIsNone(conn.execute(text("SELECT send_after FROM broadcast_logs")).scalar_one())
                migration.downgrade()
                migration.downgrade()
                self.assertNotIn("send_after", {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")})
            self.assertEqual(conn.execute(text("SELECT text FROM broadcast_logs")).scalar_one(), "kept")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
