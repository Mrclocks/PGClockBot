"""Subscription selection upgrades preserve rows and require explicit reselection."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from tests.postgres_support import isolated_database


def load_migration(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SubscriptionMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_upgrade_and_downgrade_keep_legacy_and_new_rows_with_foreign_keys(self) -> None:
        async with isolated_database() as database:
            async with database.engine.begin() as connection:
                await connection.run_sync(self.check_migration)

    def check_migration(self, connection: sa.Connection) -> None:
        migration = load_migration("0058_monitor_subscription_configs")
        connection.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO bot_users (id) VALUES (2)"))
        with Operations.context(MigrationContext.configure(connection)):
            load_migration("0055_connection_monitor").upgrade()
            connection.execute(sa.text("INSERT INTO connection_monitor_states (id, enabled) VALUES (1, true)"))
            connection.execute(sa.text("INSERT INTO connection_monitor_targets (id, pg_host_id, test_enabled, is_public, public_name, generation, endpoint_count) VALUES (1, 41, true, true, 'legacy-name', 'old', 1)"))
            connection.execute(sa.text("INSERT INTO connection_monitor_snapshots (id, target_id, endpoint_key) VALUES (1, 1, 'legacy-snapshot')"))
            migration.upgrade()
            migration.upgrade()
            self.assertEqual(connection.execute(sa.text("SELECT pg_host_id, test_enabled, is_public, public_name, subscription_key FROM connection_monitor_targets WHERE id = 1")).one(),
                             (41, False, False, "legacy-name", None))
            self.assertEqual(connection.execute(sa.text("SELECT endpoint_key FROM connection_monitor_snapshots WHERE id = 1")).scalar_one(), "legacy-snapshot")
            self.assertFalse(connection.execute(sa.text("SELECT enabled FROM connection_monitor_states WHERE id = 1")).scalar_one())
            connection.execute(sa.text("UPDATE connection_monitor_states SET enabled = true WHERE id = 1"))
            connection.execute(sa.text("INSERT INTO connection_monitor_targets (id, subscription_key, config_name, test_enabled, is_public, public_name) VALUES (3, 'native-key', 'native config', true, true, 'new-name')"))
            connection.execute(sa.text("INSERT INTO connection_monitor_snapshots (id, target_id, endpoint_key) VALUES (2, 3, 'native-snapshot')"))
            migration.upgrade()
            self.assertTrue(connection.execute(sa.text("SELECT enabled FROM connection_monitor_states WHERE id = 1")).scalar_one())
            self.assertTrue(connection.execute(sa.text("SELECT is_public FROM connection_monitor_targets WHERE id = 3")).scalar_one())
            with self.assertRaises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(sa.text("INSERT INTO connection_monitor_targets (id, subscription_key) VALUES (4, 'native-key')"))
            connection.execute(sa.text("INSERT INTO connection_monitor_targets (id, reseller_id, subscription_key) VALUES (5, 2, 'native-key')"))
            with self.assertRaises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(sa.text("INSERT INTO connection_monitor_targets (id, reseller_id, subscription_key) VALUES (6, 2, 'native-key')"))
            migration.downgrade()
            migration.downgrade()
            self.assertEqual(connection.execute(sa.text("SELECT count(*) FROM connection_monitor_targets")).scalar_one(), 3)
            self.assertEqual(connection.execute(sa.text("SELECT count(*) FROM connection_monitor_snapshots")).scalar_one(), 2)
            self.assertEqual(connection.execute(sa.text("SELECT pg_host_id, test_enabled, is_public, public_name FROM connection_monitor_targets WHERE id = 3")).one(),
                             (-3, False, False, "new-name"))
            columns = {column["name"]: column for column in sa.inspect(connection).get_columns("connection_monitor_targets")}
            self.assertNotIn("subscription_key", columns)
            self.assertFalse(columns["pg_host_id"]["nullable"])
            migration.upgrade()
            self.assertEqual(connection.execute(sa.text("SELECT count(*) FROM connection_monitor_snapshots")).scalar_one(), 2)
            if connection.dialect.name == "sqlite":
                self.assertEqual(connection.execute(sa.text("PRAGMA foreign_keys")).scalar_one(), 1)
                self.assertEqual(connection.execute(sa.text("PRAGMA foreign_key_check")).all(), [])
