"""Live service status reads the panel for all services at once, never stalls on a slow panel,
and the bot answers the tap before it waits on the panel."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import admin
from app.db.models import Base, BotUser, UserService
from app.services import bot_user_admin
from app.services.pasarguard import PasarGuardError

PANEL_DELAY = 0.3


class _Panel:
    def __init__(self, *, delay: float = PANEL_DELAY, fail: dict[int, Exception] | None = None) -> None:
        self.delay, self.fail = delay, fail or {}
        self.running = self.peak = 0

    async def get_user_by_id(self, pg_user_id: int) -> dict:
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
            if pg_user_id in self.fail:
                raise self.fail[pg_user_id]
            return {"status": "active", "used_traffic": 0, "data_limit": 10 * 1024**3, "expire": None}
        finally:
            self.running -= 1


class SnapshotCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 's.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(BotUser(id=10, telegram_id=5010, referral_code="c10"))
        await self.session.flush()
        self.session.add_all(
            UserService(id=i, bot_user_id=10, pg_username=f"u{i}", pg_user_id=(i if i != 6 else None))
            for i in range(1, 7)
        )
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def snapshots(self, panel: _Panel):
        with patch.object(bot_user_admin, "get_pg", return_value=panel):
            started = time.monotonic()
            snaps = await bot_user_admin.list_service_snapshots(self.session, 10, shop_id=None)
            return snaps, time.monotonic() - started


class SnapshotTests(SnapshotCase):
    async def test_many_services_take_about_one_panel_read_not_one_per_service(self) -> None:
        panel = _Panel()
        snaps, elapsed = await self.snapshots(panel)
        self.assertEqual(len(snaps), 6)
        # Five panel users, four at a time: two rounds, not five.
        self.assertLess(elapsed, PANEL_DELAY * 3)
        self.assertEqual(panel.peak, bot_user_admin.SNAPSHOT_CONCURRENCY)
        self.assertEqual([s.status_fa for s in snaps if s.service.pg_user_id], ["فعال"] * 5)

    async def test_a_service_without_a_panel_user_is_shown_without_asking_the_panel(self) -> None:
        snaps, _ = await self.snapshots(_Panel())
        unlinked = next(s for s in snaps if s.service.id == 6)
        self.assertEqual(unlinked.status_fa, "بدون پنل")

    async def test_a_slow_or_failing_panel_read_marks_only_that_service(self) -> None:
        panel = _Panel(fail={2: PasarGuardError("Unauthorized", 401), 3: asyncio.TimeoutError()})
        snaps, _ = await self.snapshots(panel)
        by_id = {s.service.id: s for s in snaps}
        self.assertEqual(by_id[3].error, "پاسارگارد پاسخ نداد")
        self.assertIsNotNone(by_id[2].error)
        self.assertNotIn("Traceback", by_id[2].error)
        self.assertIsNone(by_id[1].error)

    async def test_a_panel_that_never_answers_is_cut_off(self) -> None:
        with patch.object(bot_user_admin, "SNAPSHOT_TIMEOUT_SECONDS", 0.05):
            snaps, elapsed = await self.snapshots(_Panel(delay=5))
        self.assertLess(elapsed, 1.0)
        self.assertTrue(all(s.error for s in snaps))

    async def test_a_shop_service_whose_shop_has_no_panel_admin_shows_an_error_not_a_crash(self) -> None:
        service = await self.session.get(UserService, 1)
        with patch.object(bot_user_admin, "_pg_client_for_bot_service",
                          AsyncMock(side_effect=PasarGuardError("ادمین پاسارگارد برای این نماینده تعریف نشده"))):
            snap = await bot_user_admin.service_snapshot(self.session, service)
        self.assertEqual(snap.status_fa, "خطا")
        self.assertIn("ادمین پاسارگارد", snap.error)


class TapIsAnsweredFirstTests(unittest.IsolatedAsyncioTestCase):
    async def _tap(self, handler, data: str, panel_call: str, result) -> list[str]:
        order: list[str] = []
        callback = MagicMock(data=data)
        callback.answer = AsyncMock(side_effect=lambda *a, **k: order.append("answer"))
        callback.message = MagicMock()

        async def slow_panel(*_a, **_k):
            order.append("panel")
            return result

        with patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)), \
                patch.object(admin, "_is_admin", MagicMock(return_value=True)), \
                patch.object(admin, "_platform_shop_user", AsyncMock(return_value=(MagicMock(), None))), \
                patch.object(admin, "safe_edit_text", AsyncMock()), \
                patch.multiple(bot_user_admin, **{panel_call: slow_panel}, snapshot_telegram_lines=MagicMock(return_value="x")):
            await handler(callback, AsyncMock(), MagicMock(role="admin"))
        return order

    async def test_the_service_card_tap_is_answered_before_the_panel_read(self) -> None:
        snap = SimpleNamespace(service=SimpleNamespace(id=2))
        with patch.object(bot_user_admin, "get_owned_service", AsyncMock(return_value=MagicMock())):
            order = await self._tap(admin.adm_users_service_one, "adm:users:svc:10:2", "service_snapshot", snap)
        self.assertEqual(order, ["answer", "panel"])

    async def test_the_services_list_tap_is_answered_before_the_panel_reads(self) -> None:
        order = await self._tap(admin.adm_users_services, "adm:users:svcs:10", "list_service_snapshots", [])
        self.assertEqual(order, ["answer", "panel"])


if __name__ == "__main__":
    unittest.main()
