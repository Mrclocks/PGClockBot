"""In-panel update: every step before the restart is checked, and failures roll back.

A bare git repository stands in for GitHub and a fake interpreter stands in for
pip, the import check and the migration, so each step can be made to fail. No
network, systemd or database is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import panel_update as pu
from app.services import panel_update_guard as guard
from app.services.panel_update_archive import overlay

ROOT = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

# Records each call with the commit checked out at that moment; a step fails when
# the matching file exists in the control directory.
FAKE_PYTHON = """#!/usr/bin/env bash
ctl="{ctl}"
head=$(git rev-parse --short HEAD 2>/dev/null)
case "$*" in
  *"-m pip install"*) step=pip ;;
  *"import app.main"*) step=import ;;
  *"init_db"*) step=migrate ;;
  *) step=other ;;
esac
echo "$step $head" >> "$ctl/calls.log"
if [[ "$step" == migrate && -f "$ctl/migrate_moves_schema" ]]; then echo 0200 > "$ctl/schema"; fi
if [[ -f "$ctl/fail_$step" ]]; then echo "boom: $step failed (password=hunter2)" >&2; exit 1; fi
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    out = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _load_boot_guard():
    spec = importlib.util.spec_from_file_location("update_boot_guard", ROOT / "scripts" / "update_boot_guard.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _GitInstall(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        for key, value in GIT_ENV.items():
            patcher = patch.dict(os.environ, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="pgclock-panel-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.author = self.tmp / "author"
        _git(self.tmp, "clone", "-q", str(self.origin), str(self.author))
        _git(self.author, "checkout", "-q", "-b", "main")
        self.old = self.publish("0.1.0", requirements="demo==1\n")
        self.install = self.tmp / "install"
        _git(self.tmp, "clone", "-q", "-b", "main", str(self.origin), str(self.install))
        (self.install / ".env").write_text('BOT_TOKEN="secret"\n', encoding="utf-8")
        self.new = self.publish("0.2.0", requirements="demo==2\n")
        self.ctl = self.tmp / "ctl"
        self.ctl.mkdir()
        (self.ctl / "schema").write_text("0100\n", encoding="utf-8")

    def publish(self, version: str, *, requirements: str) -> str:
        (self.author / "VERSION").write_text(version + "\n", encoding="utf-8")
        (self.author / "requirements.txt").write_text(requirements, encoding="utf-8")
        (self.author / "run.py").write_text("", encoding="utf-8")
        _git(self.author, "add", "-A")
        _git(self.author, "commit", "-q", "-m", version)
        _git(self.author, "push", "-q", "origin", "main")
        return _git(self.author, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git(self.install, "rev-parse", "HEAD")

    def fail(self, step: str) -> None:
        (self.ctl / f"fail_{step}").write_text("", encoding="utf-8")

    def calls(self) -> list[str]:
        log = self.ctl / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


class PanelUpdateFlowTests(_GitInstall):
    def setUp(self) -> None:
        super().setUp()
        self.data = self.tmp / "data"
        self.data.mkdir()
        fake_py = self.tmp / "python"
        fake_py.write_text(FAKE_PYTHON.format(ctl=self.ctl), encoding="utf-8")
        fake_py.chmod(0o755)
        self.restarts: list[str] = []
        self.backups: list[str] = []
        schema_file = self.ctl / "schema"
        patches = [
            patch.object(pu, "STATUS_FILE", self.data / "panel_update.json"),
            patch.object(pu, "DATA_DIR", self.data),
            patch.object(pu, "_repo_root", return_value=self.install),
            patch.object(pu, "python_for", return_value=str(fake_py)),
            patch.object(pu, "backup_database", side_effect=self._backup),
            patch.object(pu, "read_schema_revision", side_effect=lambda: schema_file.read_text().strip()),
            patch.object(pu, "_restart_service", side_effect=self._restart),
            patch.object(pu, "setup_updated_monitor_core"),
            patch.object(pu, "clear_update_cache"),
            patch.object(pu.time, "sleep"),
            patch.object(guard, "BOOT_MARKER", self.data / "update_pending.json"),
            patch("app.services.update_channel.get_update_channel", return_value="main"),
            patch("app.services.update_channel.set_deployed_channel"),
            patch("app.services.panel_update_guard.read_schema_revision", side_effect=lambda: schema_file.read_text().strip()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _backup(self, *, log) -> str:
        if (self.ctl / "fail_backup").exists():
            raise guard.UpdateStepError("پشتیبان‌گیری قبل از آپدیت ناموفق بود؛ چیزی تغییر نکرد")
        self.backups.append("b1")
        return "b1"

    def _restart(self) -> tuple[bool, str]:
        self.restarts.append(self.head())
        return True, "restart scheduled"

    def status(self) -> dict:
        return json.loads((self.data / "panel_update.json").read_text(encoding="utf-8"))

    def marker(self) -> dict | None:
        path = self.data / "update_pending.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def test_successful_update_installs_packages_before_switching_code(self) -> None:
        pu._do_update("0.2.0")
        self.assertEqual(self.head(), self.new)
        self.assertEqual(self.calls(), [f"pip {self.old[:7]}", f"import {self.new[:7]}", f"migrate {self.new[:7]}"])
        self.assertEqual(self.restarts, [self.new])
        self.assertTrue(self.status()["awaiting_restart"])
        self.assertEqual(self.marker()["previous"]["commit"], self.old)
        self.assertEqual(self.marker()["new_commit"], self.new)
        self.assertEqual(_git(self.install, "rev-parse", "--abbrev-ref", "main@{upstream}"), "origin/main")
        self.assertEqual((self.install / ".env").read_text(encoding="utf-8"), 'BOT_TOKEN="secret"\n')

    def test_failed_backup_changes_nothing(self) -> None:
        self.fail("backup")
        pu._do_update("0.2.0")
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.status()["state"], "error")
        self.assertEqual(self.restarts, [])

    def test_failed_packages_keep_running_version_untouched(self) -> None:
        self.fail("pip")
        pu._do_update("0.2.0")
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.status()["state"], "error")
        self.assertNotIn("hunter2", json.dumps(self.status(), ensure_ascii=False))
        self.assertIsNone(self.marker())

    def test_failed_import_check_restores_previous_code_and_packages(self) -> None:
        self.fail("import")
        pu._do_update("0.2.0")
        self.assertEqual(self.head(), self.old)
        self.assertEqual(_git(self.install, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.calls()[-1], f"pip {self.old[:7]}")
        self.assertIn("نسخهٔ قبلی برگردانده شد", self.status()["error"])
        self.assertIsNone(self.marker())

    def test_failed_migration_with_unchanged_schema_rolls_back(self) -> None:
        self.fail("migrate")
        pu._do_update("0.2.0")
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.restarts, [])
        self.assertIn("نسخهٔ قبلی برگردانده شد", self.status()["error"])

    def test_failed_migration_that_moved_schema_keeps_code_and_names_backup(self) -> None:
        self.fail("migrate")
        (self.ctl / "migrate_moves_schema").write_text("", encoding="utf-8")
        pu._do_update("0.2.0")
        self.assertEqual(self.head(), self.new)
        self.assertEqual(self.restarts, [])
        self.assertIn("b1", self.status()["error"])

    def test_unreachable_github_changes_nothing(self) -> None:
        _git(self.install, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        with patch.object(pu, "download_archive", side_effect=RuntimeError("دانلود آرشیو ناموفق")):
            pu._do_update("0.2.0")
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.status()["state"], "error")
        self.assertEqual(self.restarts, [])


class ArchiveOverlayTests(unittest.TestCase):
    def test_restore_puts_replaced_files_back_and_removes_added_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, src = base / "root", base / "src"
            (root / "app").mkdir(parents=True)
            (root / "app" / "a.py").write_text("old", encoding="utf-8")
            (root / ".env").write_text("secret", encoding="utf-8")
            (src / "app").mkdir(parents=True)
            (src / "app" / "a.py").write_text("new", encoding="utf-8")
            (src / "app" / "b.py").write_text("added", encoding="utf-8")
            (src / ".env").write_text("from-archive", encoding="utf-8")

            snapshot = overlay(src, root, base / "saved", log=lambda _: None)
            self.assertEqual((root / "app" / "a.py").read_text(encoding="utf-8"), "new")
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "secret")

            snapshot.restore()
            self.assertEqual((root / "app" / "a.py").read_text(encoding="utf-8"), "old")
            self.assertFalse((root / "app" / "b.py").exists())


class BootGuardTests(_GitInstall):
    def setUp(self) -> None:
        super().setUp()
        self.guard = _load_boot_guard()
        (self.install / "data").mkdir()
        _git(self.install, "fetch", "-q", "origin")
        _git(self.install, "reset", "-q", "--hard", self.new)
        self.write_marker(attempts=0)

    def write_marker(self, *, attempts: int, new_commit: str | None = None) -> None:
        payload = {
            "previous": {"commit": self.old, "branch": "main", "schema": "0100"},
            "new_commit": new_commit or self.new,
            "backup_id": "b1",
            "attempts": attempts,
        }
        (self.install / "data" / "update_pending.json").write_text(json.dumps(payload), encoding="utf-8")

    def run_guard(self, schema: str = "0100") -> str:
        return self.guard.run(self.install, schema_reader=lambda _root: schema)

    def test_new_version_gets_three_starts_before_rollback(self) -> None:
        self.assertEqual([self.run_guard() for _ in range(3)], ["attempt-1", "attempt-2", "attempt-3"])
        self.assertEqual(self.head(), self.new)
        self.assertEqual(self.run_guard(), "rolled-back")
        self.assertEqual(self.head(), self.old)
        self.assertFalse((self.install / "data" / "update_pending.json").exists())
        status = json.loads((self.install / "data" / "panel_update.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "error")
        self.assertEqual(self.run_guard(), "no-pending-update")

    def test_moved_schema_is_not_rolled_back(self) -> None:
        self.write_marker(attempts=3)
        self.assertEqual(self.run_guard(schema="0200"), "schema-moved")
        self.assertEqual(self.head(), self.new)
        status = json.loads((self.install / "data" / "panel_update.json").read_text(encoding="utf-8"))
        self.assertIn("b1", status["error"])

    def test_marker_for_other_code_is_dropped(self) -> None:
        self.write_marker(attempts=3, new_commit=self.old)
        self.assertEqual(self.run_guard(), "stale-marker")
        self.assertEqual(self.head(), self.new)

    def test_confirmed_boot_removes_marker(self) -> None:
        marker = self.install / "data" / "update_pending.json"
        with patch.object(guard, "BOOT_MARKER", marker):
            guard.confirm_boot()
        self.assertFalse(marker.exists())
        self.assertEqual(self.run_guard(), "no-pending-update")


if __name__ == "__main__":
    unittest.main()
