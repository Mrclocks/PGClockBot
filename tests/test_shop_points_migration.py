"""0061 moves a shop customer's points to that shop and keeps everyone else's on the platform."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.postgres_support import scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]


def _load_migration():
    spec = importlib.util.spec_from_file_location("shop_points_migration", ROOT / "alembic/versions/0061_shop_points.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShopPointsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.migration = _load_migration()
        self.engine = scoped_sync_engine()
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY, reseller_id INTEGER, points_balance INTEGER DEFAULT 0)"))
            conn.execute(sa.text("CREATE TABLE points_transactions (id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER)"))
            conn.execute(sa.text("INSERT INTO bot_users VALUES (5, NULL, 0), (1, NULL, 40), (2, 5, 25), (3, 5, 0)"))
            conn.execute(sa.text("INSERT INTO points_transactions VALUES (1, 1, 40), (2, 2, 25), (3, 3, 0)"))

    def run_migration(self, conn, *, down: bool = False):
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def balances(self, conn):
        platform = dict(conn.execute(sa.text("SELECT id, points_balance FROM bot_users")).all())
        shops = {(u, s): b for u, s, b in conn.execute(sa.text("SELECT user_id, reseller_id, balance FROM shop_points"))}
        return platform, shops

    def test_a_shop_customers_points_move_to_the_shop(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            platform, shops = self.balances(conn)
            stamped = dict(conn.execute(sa.text("SELECT id, reseller_id FROM points_transactions")).all())
        self.assertEqual(platform, {5: 0, 1: 40, 2: 0, 3: 0})
        self.assertEqual(shops, {(2, 5): 25})
        self.assertEqual(stamped, {1: None, 2: 5, 3: 5})

    def test_running_it_twice_changes_nothing(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            before = self.balances(conn)
            self.run_migration(conn)
            self.assertEqual(self.balances(conn), before)

    def test_downgrade_returns_every_point_to_the_customer(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            self.run_migration(conn, down=True)
            inspector = sa.inspect(conn)
            self.assertNotIn("shop_points", inspector.get_table_names())
            self.assertNotIn("reseller_id", {c["name"] for c in inspector.get_columns("points_transactions")})
            platform = dict(conn.execute(sa.text("SELECT id, points_balance FROM bot_users")).all())
            self.assertEqual(platform, {5: 0, 1: 40, 2: 25, 3: 0})
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM points_transactions")).scalar(), 3)
            self.run_migration(conn)
            self.assertEqual(self.balances(conn)[1], {(2, 5): 25})

    def test_large_tables_are_processed_in_batches(self):
        size = self.migration.BATCH_SIZE * 2 + 3
        with self.engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO bot_users (id, reseller_id, points_balance) VALUES (:id, 5, 1)"),
                [{"id": 100 + i} for i in range(size)],
            )
            self.run_migration(conn)
            moved = conn.execute(sa.text("SELECT COUNT(*) FROM shop_points")).scalar()
            left = conn.execute(sa.text("SELECT COUNT(*) FROM bot_users WHERE reseller_id = 5 AND points_balance <> 0")).scalar()
        self.assertEqual((moved, left), (size + 1, 0))
