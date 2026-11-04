"""Persisted PasarGuard link-health summary across restarts."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import pg_link_health as health

ROOT = Path(__file__).resolve().parents[1]


class PersistLinkHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'persist.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        health.reset()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()
        health.reset()

    async def test_persist_is_debounced_to_once_per_minute(self) -> None:
        health.record_success(0.05)
        health.record_failure("timeout")
        async with self.Session() as session:
            first = await health.maybe_persist_link_health(session, pause_count=2)
            second = await health.maybe_persist_link_health(session, pause_count=3)
        self.assertTrue(first)
        self.assertFalse(second)

    async def test_card_shows_persisted_values_when_memory_empty(self) -> None:
        health.record_success(0.02)
        health.record_failure("connect")
        health.reset_persist_debounce()
        async with self.Session() as session:
            await health.maybe_persist_link_health(session, pause_count=4, force=True)
        health.reset()
        self.assertTrue(health.memory_is_empty())
        async with self.Session() as session:
            snap = await health.load_persisted_snapshot(
                session, {"paused": False, "retry_in_sec": 0, "pause_count": 0},
            )
        self.assertTrue(snap["from_prior_boot"])
        self.assertEqual(snap["prior_boot_note"], health.PRIOR_BOOT_NOTE)
        self.assertIsNotNone(snap["last_ok_at"])
        self.assertIsNotNone(snap["last_failure_at"])
        self.assertEqual(snap["last_failure"], health.KIND_LABELS["connect"])
        self.assertEqual(snap["circuit"]["pause_count"], 4)
        self.assertTrue(any(row["kind"] == "connect" for row in snap["recent_failures"]))

    async def test_live_memory_is_preferred_over_persisted(self) -> None:
        health.record_failure("dns")
        health.reset_persist_debounce()
        async with self.Session() as session:
            await health.maybe_persist_link_health(session, pause_count=1, force=True)
        health.reset()
        health.record_success(0.01)
        async with self.Session() as session:
            snap = await health.load_persisted_snapshot(session)
        self.assertFalse(snap["from_prior_boot"])
        self.assertIsNotNone(snap["last_ok_at"])
        self.assertIsNone(snap["last_failure_at"])


class PgLinkHealthStateMigrationTests(unittest.TestCase):
    def test_sqlite_upgrade_repeat_and_downgrade(self) -> None:
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "pg_link_health_state_migration",
            ROOT / "alembic/versions/0084_pg_link_health_state.py",
        )
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIn("pg_link_health_state", sa.inspect(conn).get_table_names())
                cols = {c["name"] for c in sa.inspect(conn).get_columns("pg_link_health_state")}
                self.assertTrue(
                    {"id", "last_ok_at", "last_failure_at", "last_failure_kind",
                     "pause_count", "kind_totals_24h", "updated_at"}.issubset(cols)
                )
                conn.execute(
                    text(
                        "INSERT INTO pg_link_health_state "
                        "(id, pause_count, kind_totals_24h, updated_at) "
                        "VALUES (1, 0, '{}', CURRENT_TIMESTAMP)"
                    )
                )
                migration.downgrade()
                self.assertNotIn("pg_link_health_state", sa.inspect(conn).get_table_names())
                migration.upgrade()
                self.assertIn("pg_link_health_state", sa.inspect(conn).get_table_names())
        engine.dispose()


class MergePersistedUnitTests(unittest.TestCase):
    def test_merge_marks_prior_boot_note(self) -> None:
        live = health.snapshot()
        when = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
        merged = health.merge_persisted_snapshot(
            live,
            last_ok_at=when,
            last_failure_at=when,
            last_failure_kind="timeout",
            pause_count=2,
            kind_totals_24h='{"timeout":3}',
        )
        self.assertTrue(merged["from_prior_boot"])
        self.assertEqual(merged["prior_boot_note"], "از قبل از آخرین راه‌اندازی")
        self.assertEqual(merged["last_failure"], health.KIND_LABELS["timeout"])
        self.assertEqual(merged["recent_failure_count"], 3)


if __name__ == "__main__":
    unittest.main()
