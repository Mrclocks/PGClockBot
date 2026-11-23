"""Backups use the client tools of the database server's own PostgreSQL version when installed.

On Debian and Ubuntu /usr/bin/pg_restore runs the newest installed client. A dump from client 17 sets
``transaction_timeout``, which a version 16 server rejects, so a backup made that way never restores.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import backup_postgres
from app.services.backup_postgres import RESTORE_FAILED_MESSAGE, VERSION_MESSAGE, find_tool

CI_RESTORE_ERROR = (
    'pg_restore: error: could not execute query: ERROR:  unrecognized configuration parameter "transaction_timeout"'
)


def _tool(root: Path, major: int, name: str) -> Path:
    path = root / str(major) / "bin" / name
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ToolChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for target, value in ((backup_postgres, "_VERSIONED_TOOL_ROOT"),):
            patcher = patch.object(target, value, self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        which = patch.object(backup_postgres.shutil, "which", return_value="/usr/bin/pg_restore")
        which.start()
        self.addCleanup(which.stop)

    def test_the_servers_own_version_wins_over_the_newest_client(self) -> None:
        own = _tool(self.root, 16, "pg_restore")
        _tool(self.root, 17, "pg_restore")
        self.assertEqual(find_tool("pg_restore", server_major=16), str(own))

    def test_without_that_version_installed_the_default_client_is_used(self) -> None:
        _tool(self.root, 17, "pg_restore")
        self.assertEqual(find_tool("pg_restore", server_major=15), "/usr/bin/pg_restore")

    def test_an_unknown_server_version_keeps_the_previous_choice(self) -> None:
        _tool(self.root, 16, "pg_restore")
        self.assertEqual(find_tool("pg_restore", server_major=None), "/usr/bin/pg_restore")

    def test_a_non_executable_file_is_not_chosen(self) -> None:
        path = _tool(self.root, 16, "pg_restore")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        if os.access(path, os.X_OK):  # running as root: every file is executable to the test
            self.skipTest("root can execute any file")
        self.assertEqual(find_tool("pg_restore", server_major=16), "/usr/bin/pg_restore")


class FailureMessageTests(unittest.TestCase):
    def test_a_parameter_the_older_server_does_not_know_reads_as_a_version_mismatch(self) -> None:
        self.assertEqual(backup_postgres._failure_message(CI_RESTORE_ERROR, RESTORE_FAILED_MESSAGE), VERSION_MESSAGE)

    def test_other_restore_errors_keep_the_generic_message(self) -> None:
        stderr = 'pg_restore: error: could not execute query: ERROR:  relation "x" does not exist'
        self.assertEqual(backup_postgres._failure_message(stderr, RESTORE_FAILED_MESSAGE), RESTORE_FAILED_MESSAGE)


class ServerVersionTests(unittest.TestCase):
    def test_the_major_version_is_read_from_the_server(self) -> None:
        from tests.postgres_support import scoped_database_url

        settings = backup_postgres.get_settings().model_copy(update={"database_url": scoped_database_url()})
        with patch.object(backup_postgres, "get_settings", return_value=settings):
            major = backup_postgres.server_major_version()
        self.assertIsInstance(major, int)
        self.assertGreaterEqual(major, 13)

    def test_an_unreachable_server_gives_none_not_an_error(self) -> None:
        settings = backup_postgres.get_settings().model_copy(
            update={"database_url": "postgresql+asyncpg://nobody:x@127.0.0.1:9/none"}
        )
        with patch.object(backup_postgres, "get_settings", return_value=settings):
            self.assertIsNone(backup_postgres.server_major_version())


if __name__ == "__main__":
    unittest.main()
