"""The daily report goes out once a day even when the bot missed the configured hour."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Setting
from app.jobs import scheduler
from app.services.daily_report import is_report_due, report_hour
from app.services.users import clear_settings_cache


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 31, hour, minute, tzinfo=timezone.utc)


class DueTests(unittest.TestCase):
    def test_due_from_the_configured_hour_until_the_end_of_the_day(self) -> None:
        self.assertFalse(is_report_due(at(19, 59), "20"))
        self.assertTrue(is_report_due(at(20, 0), "20"))
        self.assertTrue(is_report_due(at(23, 30), "20"))

    def test_unreadable_or_out_of_range_hours_are_clamped(self) -> None:
        self.assertEqual([report_hour(v) for v in ("", None, "abc", "-5", "99", " 7 ")], [0, 0, 0, 0, 23, 7])


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'r.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([Setting(key="admin_daily_report_hour", value="20"),
                             Setting(key="subordinate_report_enabled", value="0")])
            await session.commit()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.tmp.cleanup()

    async def run_at(self, now: datetime) -> MagicMock:
        bot = MagicMock(id=1)
        bot.send_message = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=501), message_id=1))
        clock = MagicMock(wraps=datetime)
        clock.now = MagicMock(return_value=now)
        clear_settings_cache()
        with patch.object(scheduler, "SessionLocal", self.Session), \
                patch("datetime.datetime", clock), \
                patch("app.config.get_settings", return_value=SimpleNamespace(admin_ids=[501])), \
                patch("app.services.daily_report.build_daily_report_outbound", AsyncMock(return_value=("📊", {}))):
            await scheduler.run_admin_daily_report(bot)
        return bot

    async def last_sent(self) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == "admin_daily_report_last"))

    async def test_a_run_after_the_missed_hour_still_sends_once(self) -> None:
        self.assertEqual((await self.run_at(at(19, 30))).send_message.await_count, 0)
        self.assertEqual((await self.run_at(at(22, 30))).send_message.await_count, 1)
        self.assertEqual(await self.last_sent(), "2026-10-31")
        self.assertEqual((await self.run_at(at(23, 0))).send_message.await_count, 0)


if __name__ == "__main__":
    unittest.main()
