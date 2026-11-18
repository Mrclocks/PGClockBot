"""Idempotent additive monitor schema and rollback on PostgreSQL."""

from __future__ import annotations

import importlib.util
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget
from tests.postgres_support import isolated_database


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class MonitorMigrations:
    modules: tuple[ModuleType, ...]

    def upgrade(self) -> None:
        for module in self.modules:
            module.upgrade()

    def downgrade(self) -> None:
        for module in reversed(self.modules):
            module.downgrade()


class MonitorMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_repeat_constraints_downgrade_preserves_existing_rows(self) -> None:
        modules = []
        for name in ("0055_connection_monitor", "0056_monitor_account_identity", "0058_monitor_subscription_configs"):
            spec = importlib.util.spec_from_file_location(name, ROOT / f"alembic/versions/{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
        migration = MonitorMigrations(tuple(modules))
        async with isolated_database() as database:
            async with database.engine.begin() as conn:
                await conn.run_sync(lambda sync: self.check_migration(sync, migration))

    def check_migration(self, conn: sa.Connection, migration: MonitorMigrations) -> None:
        conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, pg_username TEXT)"))
        conn.execute(sa.text("INSERT INTO bot_users (id) VALUES (2), (3)"))
        conn.execute(sa.text("INSERT INTO user_services VALUES (1, 'existing-service')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.modules[0].upgrade()
            conn.execute(sa.text("INSERT INTO connection_monitor_states (probe_id, enabled, pg_user_id, subscription_token_enc) VALUES ('legacy', true, 900, 'encrypted-unchanged')"))
            migration.upgrade()
            self.assertEqual(conn.execute(sa.text("SELECT enabled, subscription_token_enc, pg_username FROM connection_monitor_states WHERE probe_id = 'legacy'")).one(), (False, "encrypted-unchanged", None))
            conn.execute(sa.text("UPDATE connection_monitor_states SET enabled = true, pg_username = 'monitor-only' WHERE probe_id = 'legacy'"))
            migration.upgrade()
            self.assertTrue(conn.execute(sa.text("SELECT enabled FROM connection_monitor_states WHERE probe_id = 'legacy'")).scalar_one())
            conn.execute(sa.text("DELETE FROM connection_monitor_states WHERE probe_id = 'legacy'"))
            for model in (ConnectionMonitorState, ConnectionMonitorTarget, ConnectionMonitorSnapshot):
                columns = sa.inspect(conn).get_columns(model.__tablename__)
                self.assertEqual({column["name"] for column in columns}, set(model.__table__.columns.keys()))
                for column in columns:
                    expected = model.__table__.columns[column["name"]]
                    self.assertEqual(column["nullable"], expected.nullable)
                    if isinstance(expected.type, sa.String):
                        self.assertEqual(column["type"].length, expected.type.length)
            conn.execute(sa.text("INSERT INTO connection_monitor_states (probe_id) VALUES ('local')"))
            self.assertEqual(conn.execute(sa.text("SELECT enabled, account_revision FROM connection_monitor_states")).one(), (False, ""))
            self.assert_rejected(conn, "INSERT INTO connection_monitor_states (probe_id) VALUES ('local')")
            conn.execute(sa.text("INSERT INTO connection_monitor_states (reseller_id, probe_id) VALUES (2, 'local'), (3, 'local')"))
            self.assert_rejected(conn, "INSERT INTO connection_monitor_states (reseller_id, probe_id) VALUES (2, 'local')")
            conn.execute(sa.text("INSERT INTO connection_monitor_targets (pg_host_id) VALUES (41)"))
            self.assert_rejected(conn, "INSERT INTO connection_monitor_targets (pg_host_id) VALUES (41)")
            conn.execute(sa.text("INSERT INTO connection_monitor_targets (reseller_id, pg_host_id) VALUES (2, 41), (3, 41)"))
            self.assert_rejected(conn, "INSERT INTO connection_monitor_targets (pg_host_id, is_public) VALUES (72, true)")
            self.assert_rejected(conn, "INSERT INTO connection_monitor_targets (pg_host_id, endpoint_count) VALUES (72, 13)")
            self.assert_rejected(conn, "INSERT INTO connection_monitor_targets (pg_host_id, display_order) VALUES (72, -1)")
            target_id = conn.execute(sa.text("SELECT id FROM connection_monitor_targets WHERE reseller_id IS NULL")).scalar_one()
            for _ in range(2):
                statement = sa.text("INSERT INTO connection_monitor_snapshots (target_id, endpoint_key) VALUES (:target, 'one')")
                if _:
                    with self.assertRaises(IntegrityError):
                        with conn.begin_nested():
                            conn.execute(statement, {"target": target_id})
                else:
                    conn.execute(statement, {"target": target_id})
            migration.upgrade()
            self.assertEqual(conn.execute(sa.text("SELECT count(*) FROM connection_monitor_targets")).scalar_one(), 3)
            migration.downgrade()
            migration.downgrade()
            self.assertEqual(conn.execute(sa.text("SELECT pg_username FROM user_services")).scalar_one(), "existing-service")
            self.assertEqual(conn.execute(sa.text("SELECT count(*) FROM bot_users")).scalar_one(), 2)
            self.assertFalse(sa.inspect(conn).has_table("connection_monitor_targets"))
            migration.upgrade()
            self.assertTrue(sa.inspect(conn).has_table("connection_monitor_snapshots"))

    def assert_rejected(self, conn: sa.Connection, statement: str) -> None:
        with self.assertRaises(IntegrityError):
            with conn.begin_nested():
                conn.execute(sa.text(statement))
