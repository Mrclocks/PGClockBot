"""PasarGuard link watchdog: heal, rebuild once, honour disable and reconnect lock."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import pg_link_health as health
from app.services import pg_link_watchdog as watchdog
from app.services import pg_read_resilience as resilience
from app.services.users import set_setting


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'wd.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        health.reset()
        resilience.reset_read_circuits()
        watchdog.reset_watchdog_state()
        self.answer: object = 200
        self.calls = 0
        self.panel = MagicMock()
        self.panel.base_url = "https://panel.example"
        self.http = httpx.AsyncClient(
            base_url="https://panel.example",
            transport=httpx.MockTransport(self._handler),
        )
        self.panel._http = MagicMock(return_value=self.http)
        self.panel._headers = AsyncMock(return_value={"Authorization": "Bearer t"})
        self.panel._ensure_safe_target = AsyncMock(return_value=None)
        self.panel._client = self.http

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()
        self._tmpdir.cleanup()
        watchdog.reset_watchdog_state()
        resilience.reset_read_circuits()
        health.reset()

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return httpx.Response(int(self.answer), json={"ok": True})

    async def _tick(self):
        async with self.Session() as session:
            with patch.object(watchdog, "get_pg", return_value=self.panel):
                return await watchdog.run_watchdog_tick(session)

    async def test_job_heals_an_expired_paused_circuit(self) -> None:
        state = resilience._panel_state("https://panel.example")
        state.blocked_until = resilience.time.monotonic() + 60
        state.pause_count = 2
        result = await self._tick()
        self.assertTrue(result.ok)
        self.assertEqual(state.blocked_until, 0.0)
        self.assertEqual(state.pause_count, 0)
        self.assertEqual(self.calls, 1)

    async def test_failed_runs_do_not_raise(self) -> None:
        self.answer = httpx.ConnectError("down")
        result = await self._tick()
        self.assertTrue(result.ran)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "connect")

    async def test_consecutive_failures_trigger_exactly_one_rebuild_then_backoff(self) -> None:
        self.answer = httpx.ConnectError("down")
        rebuilt_flags = []
        with patch.object(watchdog, "rebuild_holder_http_client", return_value=True) as rebuild:
            for _ in range(watchdog.FAILURES_BEFORE_REBUILD):
                result = await self._tick()
                rebuilt_flags.append(result.rebuilt)
            self.assertEqual(rebuild.call_count, 1)
            self.assertEqual(rebuilt_flags, [False, False, True])
            # Backoff ticks skip the panel.
            calls_before = self.calls
            for _ in range(watchdog.BACKOFF_TICKS_AFTER_REBUILD):
                skipped = await self._tick()
                self.assertEqual(skipped.skipped_reason, "backoff")
            self.assertEqual(self.calls, calls_before)

    async def test_disabled_setting_skips(self) -> None:
        async with self.Session() as session:
            await set_setting(session, watchdog.SETTING_KEY, "0")
            await session.commit()
        result = await self._tick()
        self.assertEqual(result.skipped_reason, "disabled")
        self.assertEqual(self.calls, 0)

    async def test_reconnect_lock_is_respected(self) -> None:
        with patch.object(watchdog, "is_reconnect_locked", return_value=True):
            result = await self._tick()
        self.assertEqual(result.skipped_reason, "reconnect_lock")
        self.assertEqual(self.calls, 0)


class SettingDeclarationTests(unittest.TestCase):
    def test_setting_is_default_on_and_in_groups(self) -> None:
        from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS, TAB_SETTING_GROUPS

        self.assertEqual(DEFAULT_SETTINGS.get(watchdog.SETTING_KEY), "1")
        keys = {item[0] for group in SETTING_GROUPS.values() for item in group}
        self.assertIn(watchdog.SETTING_KEY, keys)
        self.assertIn("سلامت ارتباط پاسارگارد", TAB_SETTING_GROUPS.get("notifications", []))


if __name__ == "__main__":
    unittest.main()
