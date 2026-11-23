"""The PostgreSQL-only check fails on any tracked SQLite reference outside its exemptions."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "postgres_only_check.py"


def _load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("postgres_only_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class SqliteReferenceCountTests(unittest.TestCase):
    def test_counts_every_spelling(self) -> None:
        self.assertEqual(check.count_sqlite_references("SQLite sqlite+aiosqlite:// import sqlite3"), 4)

    def test_partial_index_keyword_is_no_longer_tolerated(self) -> None:
        self.assertEqual(check.count_sqlite_references('Index("ix", sqlite_where=cond, postgresql_where=cond)'), 1)


class TrackedFileScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory(prefix="pgclock-postgres-only-")
        self.repo = Path(self._folder.name)
        _git(self.repo, "init", "-q", "-b", "dev")
        self.write("app/service.py", "url = 'postgresql+asyncpg://'\n")

    def tearDown(self) -> None:
        self._folder.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def found(self) -> list[tuple[str, int]]:
        _git(self.repo, "add", "-A")
        with patch.object(check, "ROOT", self.repo):
            return check.find_references()

    def test_a_clean_tree_passes(self) -> None:
        self.assertEqual(self.found(), [])

    def test_any_tracked_file_that_mentions_it_fails(self) -> None:
        self.write("tests/test_new.py", "create_async_engine('sqlite+aiosqlite:///:memory:')\n")
        self.write("requirements.txt", "aiosqlite>=0.20\n")
        self.assertEqual(sorted(self.found()), [("requirements.txt", 1), ("tests/test_new.py", 2)])

    def test_untracked_files_are_not_scanned(self) -> None:
        _git(self.repo, "add", "-A")
        (self.repo / "scratch.py").write_text("import sqlite3\n", encoding="utf-8")
        with patch.object(check, "ROOT", self.repo):
            self.assertEqual(check.find_references(), [])

    def test_merged_migrations_and_documentation_are_exempt(self) -> None:
        self.write("alembic/versions/0001_old.py", "render_as_batch = dialect == 'sqlite'\n")
        self.write("docs/HISTORY.md", "SQLite support was removed.\n")
        self.assertEqual(self.found(), [])
