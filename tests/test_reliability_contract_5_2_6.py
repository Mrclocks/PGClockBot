"""Regression: critical-path reliability (no false disconnect, Jinja dict traps, restore status)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class RestoreStatusContract(unittest.TestCase):

    def test_validate_failure_finishes_status(self):
        """Early validate failure must not leave state=running forever."""
        from app.services import backup as backup_mod

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            status_file = data / "backup_restore.json"
            with mock.patch.object(backup_mod, "DATA_DIR", data), mock.patch.object(
                backup_mod, "RESTORE_STATUS_FILE", status_file
            ), mock.patch.object(backup_mod, "BACKUP_DIR", data / "backups"):
                (data / "backups").mkdir()
                # Seed running as async start would
                backup_mod._replace_restore_status(
                    {
                        "state": "running",
                        "message": "شروع ریستور…",
                        "started_at": backup_mod._now_iso(),
                    }
                )
                bad = data / "backups" / "nope.zip"
                bad.write_bytes(b"not-a-zip")
                result = backup_mod.restore_backup(bad, restart=False, safety_backup=False)
                self.assertFalse(result.get("ok"))
                st = backup_mod.read_restore_status()
                self.assertEqual(st.get("state"), "error", msg=st)
                self.assertNotEqual(st.get("state"), "running")

    def test_awaiting_blocks_second_start(self):
        from app.runtime import BOOT_ID
        from app.services import backup as backup_mod

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            status_file = data / "backup_restore.json"
            with mock.patch.object(backup_mod, "DATA_DIR", data), mock.patch.object(
                backup_mod, "RESTORE_STATUS_FILE", status_file
            ), mock.patch.object(backup_mod, "BACKUP_DIR", data / "backups"), mock.patch.object(
                backup_mod, "_RESTORE_THREAD", None
            ):
                (data / "backups").mkdir()
                # Same boot id → restart has not happened yet; block a second start.
                backup_mod._replace_restore_status(
                    {
                        "state": "done",
                        "awaiting_restart": True,
                        "message": "awaiting",
                        "finished_at": backup_mod._now_iso(),
                        "pre_boot_id": BOOT_ID,
                    }
                )
                zip_path = data / "backups" / "x.zip"
                zip_path.write_bytes(b"x" * 100)
                out = backup_mod.start_restore_async(zip_path, restart=False, safety_backup=False)
                self.assertFalse(out.get("ok"))
                self.assertIn("راه‌اندازی مجدد", out.get("error") or "")


if __name__ == "__main__":
    unittest.main()
