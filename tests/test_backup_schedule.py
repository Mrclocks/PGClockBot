"""The nightly backup runs off the event loop, once a day, and tells admins about problems."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEB_SECRET", "x" * 48)

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.services import backup_schedule as schedule
from app.services import backup_telegram as tg
from app.services.users import get_setting, set_setting

AT_THREE = datetime(2026, 11, 2, 3, 5, tzinfo=timezone.utc)


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.archive = Path(self._tmp.name) / "pgclock-backup-x.zip"
        self.archive.write_bytes(b"zip")
        self.bot = MagicMock()
        self.bot.send_document = AsyncMock()
        self.bot.send_message = AsyncMock()
        self.bot.session.close = AsyncMock()
        self.build_threads: list[int] = []
        self.verified = True
        for patcher in (
            patch.object(tg, "_open_bot", return_value=self.bot),
            patch.object(tg, "get_settings", return_value=SimpleNamespace(bot_token="999:PANEL", admin_ids=[11])),
            patch.object(schedule, "create_backup", side_effect=self._build),
            patch.object(schedule, "validate_backup_archive", side_effect=lambda path: (self.verified, "bad", {})),
            patch.object(schedule, "prune_backups", return_value=0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    def _build(self, **kwargs) -> dict:
        self.build_threads.append(threading.get_ident())
        return {"path": str(self.archive), "id": "x", "include_env": False}

    async def run_at(self, moment: datetime) -> str:
        async with self.Session() as session:
            return await schedule.run_nightly_backup(session, moment)

    async def setting(self, key: str) -> str:
        async with self.Session() as session:
            return await get_setting(session, key, "")

    async def enable_delivery(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "backup_telegram_enabled", "1")
            await session.commit()


class NightlyBackupTests(_Case):
    async def test_backup_is_built_off_the_event_loop(self) -> None:
        self.assertEqual(await self.run_at(AT_THREE), "ok")
        self.assertEqual(len(self.build_threads), 1)
        self.assertNotEqual(self.build_threads[0], threading.get_ident())
        self.assertEqual(await self.setting(schedule.LAST_OK_KEY), AT_THREE.isoformat())
        self.assertEqual(await self.setting(schedule.VERIFY_KEY), "1")

    async def test_other_hours_do_nothing(self) -> None:
        self.assertEqual(await self.run_at(AT_THREE.replace(hour=4)), "skipped")
        self.assertEqual(self.build_threads, [])

    async def test_only_one_attempt_per_day_even_after_a_failure(self) -> None:
        with patch.object(schedule, "create_backup", side_effect=OSError("disk full")):
            self.assertEqual(await self.run_at(AT_THREE), "failed")
        self.assertEqual(await self.run_at(AT_THREE.replace(minute=35)), "skipped")
        self.bot.send_message.assert_awaited_once()

    async def test_failed_backup_is_reported_and_not_marked_ok(self) -> None:
        with patch.object(schedule, "create_backup", side_effect=OSError("/srv/data: disk full")):
            self.assertEqual(await self.run_at(AT_THREE), "failed")
        self.assertEqual(await self.setting(schedule.LAST_OK_KEY), "")
        chat_id, text = self.bot.send_message.await_args.args
        self.assertEqual(chat_id, 11)
        self.assertEqual(text, schedule.FAILED_MESSAGE)
        self.assertNotIn("/srv", text)

    async def test_backup_that_fails_its_check_is_reported_and_not_sent(self) -> None:
        await self.enable_delivery()
        self.verified = False
        self.assertEqual(await self.run_at(AT_THREE), "unverified")
        self.assertEqual(await self.setting(schedule.LAST_OK_KEY), "")
        self.assertEqual(await self.setting(schedule.VERIFY_KEY), "0")
        self.assertEqual(self.bot.send_message.await_args.args[1], schedule.UNVERIFIED_MESSAGE)
        self.bot.send_document.assert_not_awaited()

    async def test_disabled_schedule_does_nothing(self) -> None:
        async with self.Session() as session:
            await set_setting(session, schedule.ENABLED_KEY, "0")
            await session.commit()
        self.assertEqual(await self.run_at(AT_THREE), "skipped")
        self.assertEqual(self.build_threads, [])


class DeliveryNoticeTests(_Case):
    async def test_too_large_backup_is_announced_instead_of_silently_skipped(self) -> None:
        await self.enable_delivery()
        async with self.Session() as session:
            with patch.object(tg, "MAX_UPLOAD_BYTES", 1):
                status = await tg.send_backup(session, self.archive, include_env=False, caption="c")
        self.assertEqual(status, "skipped")
        self.bot.send_document.assert_not_awaited()
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("وب‌پنل", text)
        self.assertIn("50", text.replace("۵۰", "50"))

    async def test_problem_notice_reaches_admins_with_delivery_off(self) -> None:
        async with self.Session() as session:
            delivered = await tg.notify_backup_problem(session, "x")
        self.assertEqual(delivered, 1)
        markup = self.bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual([b.callback_data for row in markup.inline_keyboard for b in row], ["nv:adm:backup"])

    async def test_problem_notice_to_a_separate_backup_bot_has_no_panel_button(self) -> None:
        destination = tg.Destination("token", [5], custom=True)
        with patch.object(tg, "resolve_destination", AsyncMock(return_value=destination)):
            async with self.Session() as session:
                await tg.notify_backup_problem(session, "x")
        self.assertIsNone(self.bot.send_message.await_args.kwargs["reply_markup"])


class BotUploadLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_over_the_bot_limit_points_to_the_web_panel(self) -> None:
        from app.bot.handlers import admin_backup

        handler = admin_backup.backup_upload_file
        message = MagicMock()
        message.answer = AsyncMock()
        message.document = SimpleNamespace(file_name="b.zip", file_size=admin_backup.BOT_DOWNLOAD_LIMIT_BYTES + 1)
        with patch("app.bot.auth.require_bot_owner", new=AsyncMock(return_value=True)), patch.object(
            admin_backup, "_is_admin", return_value=True
        ), patch.object(admin_backup, "_download_upload_bytes", new=AsyncMock()) as download:
            await handler(message, db_user=SimpleNamespace(id=1), state=AsyncMock())
        download.assert_not_awaited()
        self.assertIn("وب‌پنل", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
