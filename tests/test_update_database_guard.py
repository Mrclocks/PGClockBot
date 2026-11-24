"""Updates are refused, from the web panel and from pgclock.sh, when the database is not PostgreSQL."""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import panel_update, update_channel
from app.services import update_database_guard as guard

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = "postgresql+asyncpg://pgclock:pw@127.0.0.1:5432/pgclock"
OTHER_URLS = ("mysql://root:pw@db/pgclock", "file:///opt/data/bot.db", "")


def _write_env(folder: Path, url: str | None) -> None:
    lines = ['WEB_SECRET="x"']
    if url is not None:
        lines.append(f'DATABASE_URL="{url}"')
    (folder / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


class WebPanelGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory(prefix="pgclock-update-guard-")
        self.root = Path(self._folder.name)
        patcher = patch.object(guard, "ROOT_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._folder.cleanup)

    def test_a_postgres_database_is_not_blocked(self) -> None:
        _write_env(self.root, POSTGRES_URL)
        self.assertIsNone(guard.database_update_block())

    def test_any_other_or_missing_database_blocks_the_update_page(self) -> None:
        for url in (*OTHER_URLS, None):
            with self.subTest(url=url):
                _write_env(self.root, url)
                block = guard.database_update_block()
                self.assertTrue(block and block["blocked"])
                self.assertEqual(block["label"], "دیتابیس شما پشتیبانی نمی‌شود")
                if url:
                    self.assertNotIn(url, block["message"])

    def test_the_preflight_blocks_before_asking_github(self) -> None:
        _write_env(self.root, OTHER_URLS[0])
        remote = AsyncMock(return_value={"x"})
        with patch.object(update_channel, "fetch_remote_alembic_revision_ids", remote):
            result = asyncio.run(update_channel.migration_preflight("main"))
        self.assertTrue(result["blocked"])
        self.assertEqual(result["message"], guard.UNSUPPORTED_DATABASE_MESSAGE)
        remote.assert_not_awaited()

    def test_start_update_refuses_without_starting_anything(self) -> None:
        _write_env(self.root, OTHER_URLS[0])
        with patch.object(panel_update, "_start_update_thread") as start:
            result = panel_update.start_update("9.9.9")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], guard.UNSUPPORTED_DATABASE_MESSAGE)
        start.assert_not_called()

    def test_start_update_goes_ahead_on_postgres(self) -> None:
        _write_env(self.root, POSTGRES_URL)
        with patch.object(panel_update, "_start_update_thread", return_value={"ok": True}) as start:
            self.assertEqual(panel_update.start_update("9.9.9"), {"ok": True})
        start.assert_called_once_with("9.9.9")


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.S | re.M)
    if match is None:
        raise AssertionError(f"{name} not found in pgclock.sh")
    return match.group(0)


class ServerGuardTests(unittest.TestCase):
    """Runs the real require_postgres_database_url from pgclock.sh against a temporary .env."""

    def run_guard(self, url: str | None) -> subprocess.CompletedProcess[str]:
        source = (ROOT / "pgclock.sh").read_text(encoding="utf-8")
        script = "\n".join([
            'err() { echo "x $*"; }',
            'warn() { echo "! $*"; }',
            _shell_function(source, "env_get"),
            _shell_function(source, "require_postgres_database_url"),
            "require_postgres_database_url",
        ])
        with tempfile.TemporaryDirectory(prefix="pgclock-update-guard-sh-") as folder:
            _write_env(Path(folder), url)
            return subprocess.run(["bash", "-c", script], cwd=folder, capture_output=True, text=True, timeout=30)

    def test_postgres_lets_the_update_go_on(self) -> None:
        for url in (POSTGRES_URL, "postgresql://u:p@h/db", "postgres://u:p@h/db"):
            with self.subTest(url=url):
                self.assertEqual(self.run_guard(url).returncode, 0)

    def test_any_other_or_missing_database_stops_the_update(self) -> None:
        for url in (*OTHER_URLS, None):
            with self.subTest(url=url):
                result = self.run_guard(url)
                self.assertEqual(result.returncode, 1)
                self.assertIn("دیتابیس شما پشتیبانی نمی‌شود", result.stdout)
                self.assertIn("nothing changed", result.stdout)
