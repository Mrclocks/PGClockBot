"""The app starts only with a PostgreSQL DATABASE_URL and never echoes the URL when refusing."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPORT_SESSION = "import app.db.session as s; print('started', s.engine.dialect.name)"


def _import_session(database_url: str) -> subprocess.CompletedProcess[str]:
    # A fresh interpreter: the engine is built once, when the module is first imported.
    env = {**os.environ, "DATABASE_URL": database_url}
    env.pop("TEST_DATABASE_URL", None)
    return subprocess.run(
        [sys.executable, "-c", IMPORT_SESSION], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )


class DatabaseUrlStartupTests(unittest.TestCase):
    def test_postgres_url_builds_the_engine(self) -> None:
        result = _import_session("postgresql://pgclock:pw@127.0.0.1:5432/pgclock")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("started postgresql", result.stdout)

    def test_missing_url_is_refused(self) -> None:
        result = _import_session("")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_URL is empty", result.stderr)

    def test_other_databases_are_refused_without_echoing_the_url(self) -> None:
        result = _import_session("mysql://root:s3cret-pass@db.internal/pgclock")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be PostgreSQL", result.stderr)
        self.assertNotIn("s3cret-pass", result.stderr + result.stdout)
