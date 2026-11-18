"""The SQLite ratchet blocks new SQLite references and allows removing them."""

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


def _load_ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("postgres_only_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Dataclasses look their module up in sys.modules while the class is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_ratchet()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class SqliteReferenceCountTests(unittest.TestCase):
    def test_counts_every_spelling(self) -> None:
        self.assertEqual(ratchet.count_sqlite_references("SQLite sqlite+aiosqlite:// import sqlite3"), 4)

    def test_partial_index_keyword_is_tolerated(self) -> None:
        self.assertEqual(ratchet.count_sqlite_references('Index("ix", sqlite_where=cond, postgresql_where=cond)'), 0)


class SqliteRatchetRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory(prefix="pgclock-postgres-only-")
        self.repo = Path(self._folder.name)
        _git(self.repo, "init", "-q", "-b", "dev")
        _git(self.repo, "config", "user.email", "ratchet@example.invalid")
        _git(self.repo, "config", "user.name", "ratchet")
        self.write("app/service.py", "url = 'sqlite+aiosqlite://'\n")
        self.write("alembic/versions/0001_old.py", "x = 1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        _git(self.repo, "checkout", "-q", "-b", "feature")

    def tearDown(self) -> None:
        self._folder.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def regressions(self) -> list[tuple[str, int, int]]:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "change")
        with patch.object(ratchet, "ROOT", self.repo):
            return ratchet.find_regressions(self.base)

    def test_new_file_with_sqlite_fails(self) -> None:
        self.write("tests/test_new.py", "create_async_engine('sqlite+aiosqlite:///:memory:')\n")
        self.assertEqual(self.regressions(), [("tests/test_new.py", 0, 2)])

    def test_adding_a_reference_to_an_existing_file_fails(self) -> None:
        self.write("app/service.py", "url = 'sqlite+aiosqlite://'\nimport sqlite3\n")
        self.assertEqual(self.regressions(), [("app/service.py", 2, 3)])

    def test_removing_references_passes(self) -> None:
        self.write("app/service.py", "url = 'postgresql+asyncpg://'\n")
        self.assertEqual(self.regressions(), [])

    def test_renamed_file_keeps_its_old_count(self) -> None:
        _git(self.repo, "mv", "app/service.py", "app/renamed_service.py")
        self.assertEqual(self.regressions(), [])

    def test_merged_migrations_are_exempt(self) -> None:
        self.write("alembic/versions/0001_old.py", "x = 1\nrender_as_batch = dialect == 'sqlite'\n")
        self.assertEqual(self.regressions(), [])

    def test_documentation_is_exempt(self) -> None:
        self.write("docs/REMOVAL.md", "SQLite support is being removed.\n")
        self.assertEqual(self.regressions(), [])

    def test_deleted_file_is_ignored(self) -> None:
        (self.repo / "app/service.py").unlink()
        self.assertEqual(self.regressions(), [])
