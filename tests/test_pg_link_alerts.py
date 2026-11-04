"""Owner alerts when the PasarGuard link goes down or recovers."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import pg_link_alerts as alerts
from app.services import pg_link_health as health
from app.services import pg_read_resilience as resilience
from app.services.users import get_setting, set_setting


class LinkAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'alerts.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        health.reset()
        resilience.reset_read_circuits()
        self.now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        self.sent: list[str] = []

        async def fake_send(text: str) -> int:
            self.sent.append(text)
            return 1

        self._send_patch = patch.object(alerts, "_send_to_owners", side_effect=fake_send)
        self._send_patch.start()

    async def asyncTearDown(self) -> None:
        self._send_patch.stop()
        await self.engine.dispose()
        self._tmpdir.cleanup()
        resilience.reset_read_circuits()
        health.reset()

    async def _notify(self, *, is_down: bool, kind: str | None = "connect", now: datetime | None = None) -> str | None:
        async with self.Session() as session:
            return await alerts.notify_link_health_change(
                session, is_down=is_down, failure_kind=kind, now=now or self.now,
            )

    async def test_healthy_to_down_sends_one_message(self) -> None:
        result = await self._notify(is_down=True, kind="connect")
        self.assertEqual(result, alerts.ALERT_DOWN)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(health.KIND_LABELS["connect"], self.sent[0])
        # 12:00 UTC → 15:30 Asia/Tehran
        self.assertIn("15:30:00", self.sent[0])

    async def test_flapping_within_throttle_does_not_spam(self) -> None:
        await self._notify(is_down=True)
        await self._notify(is_down=False, now=self.now + timedelta(seconds=30))
        await self._notify(is_down=True, now=self.now + timedelta(minutes=1))
        await self._notify(is_down=False, now=self.now + timedelta(minutes=2))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("قطع", self.sent[0])
        self.assertIn("برقرار شد", self.sent[1])

    async def test_recovery_once_per_outage(self) -> None:
        await self._notify(is_down=True)
        first = await self._notify(is_down=False, now=self.now + timedelta(seconds=10))
        second = await self._notify(is_down=False, now=self.now + timedelta(seconds=20))
        self.assertEqual(first, alerts.ALERT_UP)
        self.assertIsNone(second)
        self.assertEqual(sum(1 for t in self.sent if "برقرار شد" in t), 1)

    async def test_dynamic_parts_are_html_escaped(self) -> None:
        # Inject a hostile label via a temporary kind map entry is impossible;
        # compose_down_text still escapes the fixed label and timestamp digits.
        hostile_stamp = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        with patch.object(alerts, "_tehran_stamp", return_value='12:00<script>x</script>'):
            text = alerts.compose_down_text(failure_kind="timeout", when=hostile_stamp)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    async def test_notify_failure_does_not_raise(self) -> None:
        self._send_patch.stop()

        async def boom(_text: str) -> int:
            raise RuntimeError("telegram down")

        with patch.object(alerts, "_send_to_owners", side_effect=boom):
            async with self.Session() as session:
                result = await alerts.notify_link_health_change(
                    session, is_down=True, failure_kind="dns", now=self.now,
                )
        self.assertIsNone(result)
        self._send_patch.start()

    async def test_restart_keeps_throttle_from_persisted_settings(self) -> None:
        await self._notify(is_down=True)
        self.assertEqual(len(self.sent), 1)
        # Simulate process restart: in-memory gone, settings remain.
        async with self.Session() as session:
            outage = await get_setting(session, alerts.OUTAGE_ACTIVE_KEY, "0")
            last = await get_setting(session, alerts.LAST_DOWN_SENT_KEY, "")
            self.assertEqual(outage, "1")
            self.assertTrue(last)
        # Fresh call within 15m must not send another down.
        again = await self._notify(is_down=True, now=self.now + timedelta(minutes=5))
        self.assertIsNone(again)
        self.assertEqual(len(self.sent), 1)

    async def test_disabled_setting_skips(self) -> None:
        async with self.Session() as session:
            await set_setting(session, alerts.SETTING_KEY, "0")
        result = await self._notify(is_down=True)
        self.assertIsNone(result)
        self.assertEqual(self.sent, [])

    async def test_retry_after_is_honoured(self) -> None:
        self._send_patch.stop()
        calls = {"n": 0}

        class _Session:
            async def close(self) -> None:
                return None

        class FakeBot:
            def __init__(self) -> None:
                self.session = _Session()

            async def send_message(self, chat_id, text, parse_mode=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TelegramRetryAfter(method=MagicMock(), message="slow", retry_after=1)
                return MagicMock()

        import sys
        import types

        bot_mod = types.ModuleType("app.bot")
        bot_mod.create_bot = MagicMock(return_value=FakeBot())  # type: ignore[attr-defined]
        real_settings = alerts.get_settings()
        with patch("asyncio.sleep", new=AsyncMock()), \
             patch.object(alerts, "get_settings", return_value=MagicMock(admin_ids=[111])), \
             patch.dict(sys.modules, {"app.bot": bot_mod}):
            delivered = await alerts._send_to_owners("hello")
        self.assertIs(alerts.get_settings(), real_settings)
        self.assertEqual(delivered, 1)
        self.assertEqual(calls["n"], 2)
        self._send_patch.start()


class SettingDeclarationTests(unittest.TestCase):
    def test_setting_is_default_on_and_in_groups(self) -> None:
        from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS, TAB_SETTING_GROUPS

        self.assertEqual(DEFAULT_SETTINGS.get(alerts.SETTING_KEY), "1")
        keys = {item[0] for group in SETTING_GROUPS.values() for item in group}
        self.assertIn(alerts.SETTING_KEY, keys)
        self.assertIn("سلامت ارتباط پاسارگارد", TAB_SETTING_GROUPS.get("notifications", []))


class ComposeTextTests(unittest.TestCase):
    def test_down_text_uses_kind_label_not_exception(self) -> None:
        text = alerts.compose_down_text(
            failure_kind="timeout",
            when=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc),
        )
        self.assertIn(health.KIND_LABELS["timeout"], text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("Exception", text)


if __name__ == "__main__":
    unittest.main()
