"""«سلامت سیستم»: only the owner opens it, every line reads its state without breaking the page, and each
check turns its measurement into the right tone."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import OperationalError

from app.services import system_health as health
from app.services.job_metrics import JobMetrics
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

ROW = re.compile(r'data-health="(\w+)" data-tone="(\w+)"')


class HealthPageTests(RoutesHarness):
    async def get(self, session_payload: dict, path: str = "/tools/health"):
        cookie = f"session={self.signer.dumps(session_payload)}"
        return await self.client.get(path, headers={"cookie": cookie})

    async def test_the_owner_sees_every_line_and_the_list_offers_the_page(self) -> None:
        with patch.object(health, "bot_status", AsyncMock(return_value={"state": "ok", "username": "shopbot"})):
            response = await self.get(OWNER_SESSION)
        self.assertEqual(response.status_code, 200)
        stuck = ["settlements", "deliveries", "freezes", "cashback", "transfers"]
        self.assertEqual([key for key, _ in ROW.findall(response.text)], [*health.CHECKS, *stuck])
        self.assertIn('<a class="page-back" href="/tools"', response.text)
        self.assertIn('href="/tools/health"', (await self.get(OWNER_SESSION, "/tools")).text)

    async def test_a_shop_is_sent_away_and_is_not_offered_the_page(self) -> None:
        response = await self.get(SHOP_SESSION)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/home"))
        listing = await self.get(SHOP_SESSION, "/tools")
        self.assertNotIn("/tools/health", listing.text)

    async def test_a_check_that_breaks_reads_not_checked_and_the_page_still_renders(self) -> None:
        with patch.object(health, "check_disk", AsyncMock(side_effect=OSError("disk gone /secret/path"))), \
                patch.object(health, "bot_status", AsyncMock(return_value={"state": "neutral", "detail": "بررسی نشد"})):
            response = await self.get(OWNER_SESSION)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(dict(ROW.findall(response.text))["disk"], "neutral")
        self.assertNotIn("/secret/path", response.text)

    async def test_a_hostile_bot_name_is_escaped(self) -> None:
        hostile = {"state": "ok", "username": "<img src=x onerror=alert(1)>"}
        with patch.object(health, "bot_status", AsyncMock(return_value=hostile)):
            response = await self.get(OWNER_SESSION)
        self.assertNotIn("<img src=x", response.text)
        self.assertIn("&lt;img src=x", response.text)


class DatabaseCheckTests(IsolatedAsyncioTestCase):
    async def test_schema_behind_the_code_is_a_warning(self) -> None:
        session = AsyncMock()
        session.scalars.return_value = SimpleNamespace(all=lambda: ["0001_old"])
        with patch.object(health, "_code_heads", return_value=("0130_new",)):
            item = await health.check_database(session)
        self.assertEqual(item.tone, health.WARN)

    async def test_a_database_that_fails_is_an_error_and_the_session_is_rolled_back(self) -> None:
        session = AsyncMock()
        session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("password=hunter2"))
        with patch.object(health, "rollback_quiet", AsyncMock()) as rollback:
            item = await health._database(session)
        self.assertEqual(item.tone, health.ERR)
        self.assertNotIn("hunter2", item.summary)
        rollback.assert_awaited_once_with(session)


class FileCheckTests(IsolatedAsyncioTestCase):
    async def test_free_disk_thresholds(self) -> None:
        gib = 1024**3
        for free, tone in ((0.5 * gib, health.ERR), (2 * gib, health.WARN), (10 * gib, health.OK)):
            with self.subTest(free=free), patch.object(health.shutil, "disk_usage", return_value=SimpleNamespace(free=free)):
                self.assertEqual((await health.check_disk()).tone, tone)

    async def test_backup_age(self) -> None:
        with TemporaryDirectory() as folder:
            backups = Path(folder)
            with patch.object(health, "BACKUP_DIR", backups):
                self.assertEqual((await health.check_backup()).tone, health.WARN)
                archive = backups / f"{health.BACKUP_PREFIX}1.zip"
                archive.write_bytes(b"")
                self.assertEqual((await health.check_backup()).tone, health.OK)
                old = time.time() - timedelta(days=3).total_seconds()
                os.utime(archive, (old, old))
                self.assertEqual((await health.check_backup()).tone, health.WARN)

    async def test_certificate_states(self) -> None:
        now = datetime.now(timezone.utc)
        cases = (
            ({"ssl_enabled": False}, health.NEUTRAL),
            ({"ssl_enabled": True, "expires_at": None}, health.WARN),
            ({"ssl_enabled": True, "expires_at": (now - timedelta(days=1)).isoformat()}, health.ERR),
            ({"ssl_enabled": True, "expires_at": (now + timedelta(days=5)).isoformat()}, health.WARN),
            ({"ssl_enabled": True, "expires_at": (now + timedelta(days=60)).isoformat()}, health.OK),
        )
        for status, tone in cases:
            with self.subTest(status=status), patch.object(health, "cert_status", return_value=status):
                self.assertEqual((await health.check_certificate()).tone, tone)


class LiveStateCheckTests(IsolatedAsyncioTestCase):
    async def test_jobs_whose_last_run_failed_are_named(self) -> None:
        metrics = JobMetrics()
        for job in ("crypto_rate_refresh", "billing_tick"):
            metrics.mark_started(job)
            metrics.mark_finished(job, succeeded=True)
        with patch.object(health, "job_metrics", metrics):
            self.assertEqual((await health.check_jobs()).tone, health.OK)
            metrics.mark_started("billing_tick")
            metrics.mark_finished("billing_tick", succeeded=False)
            item = await health.check_jobs()
        self.assertEqual(item.tone, health.ERR)
        self.assertIn("صورت‌حساب مصرفی نماینده‌ها", item.summary)
        self.assertNotIn("نرخ رمزارز", item.summary)

    async def test_pasarguard_link_states(self) -> None:
        now = datetime.now(timezone.utc)
        cases = (
            ({"last_ok_at": None}, health.NEUTRAL),
            ({"last_ok_at": now.isoformat(), "recent_failure_count": 0}, health.OK),
            ({"last_ok_at": now.isoformat(), "recent_failure_count": 3}, health.WARN),
            ({"last_ok_at": (now - timedelta(minutes=10)).isoformat(), "recent_failure_count": 3}, health.ERR),
        )
        for snap, tone in cases:
            with self.subTest(snap=snap), patch.object(health.pg_link_health, "snapshot", return_value=snap):
                self.assertEqual((await health.check_pasarguard()).tone, tone)
