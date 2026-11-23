"""The operator CLI knows PostgreSQL only and never prints the database URL."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.cli import service_cmds
from app.cli.context import CliContext
from app.cli.main import build_parser

SECRET = "s3cret-db-password"


class MigrateCommandTests(unittest.TestCase):
    def test_migrate_no_longer_copies_from_another_database(self) -> None:
        for removed in (["--source", "x"], ["--target", "x"]):
            with self.subTest(removed[0]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["migrate", *removed])

    def test_plain_migrate_still_parses(self) -> None:
        self.assertEqual(build_parser().parse_args(["migrate"]).command, "migrate")


class StatusDatabaseLineTests(unittest.TestCase):
    def status_output(self, database_url: str) -> str:
        with tempfile.TemporaryDirectory(prefix="pgclock-cli-status-") as root:
            (Path(root) / ".env").write_text(f'DATABASE_URL="{database_url}"\n', encoding="utf-8")
            out = io.StringIO()
            with (
                patch.object(service_cmds, "service_unit_installed", return_value=False),
                patch.object(service_cmds, "_probe_health", return_value={"ok": False, "error": "down"}),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(out),
            ):
                service_cmds.cmd_status(CliContext(root=Path(root)))
            return out.getvalue()

    def test_postgres_is_named_without_its_url(self) -> None:
        output = self.status_output(f"postgresql+asyncpg://pgclock:{SECRET}@127.0.0.1:5432/pgclock")
        self.assertIn("postgresql", output)
        self.assertNotIn(SECRET, output)

    def test_an_unsupported_url_is_reported_without_printing_it(self) -> None:
        output = self.status_output(f"mysql://root:{SECRET}@db/pgclock")
        self.assertIn("not PostgreSQL", output)
        self.assertNotIn(SECRET, output)
