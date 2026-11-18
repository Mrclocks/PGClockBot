"""0060 stores the selling shop on services, keeps ambiguous ones under review and fills membership."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.postgres_support import scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
SHOP = 5


def _load_migration():
    spec = importlib.util.spec_from_file_location("service_shop_migration", ROOT / "alembic/versions/0060_service_shop_scope.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServiceShopMigrationTests(unittest.TestCase):
    def setUp(self):
        self.migration = _load_migration()
        self.engine = scoped_sync_engine()
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY, reseller_id INTEGER)"))
            conn.execute(sa.text("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, reseller_id INTEGER)"))
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, bot_user_id INTEGER, remark TEXT)"))
            conn.execute(sa.text("INSERT INTO bot_users VALUES (5, NULL), (1, NULL), (2, 5), (3, 5), (4, NULL), (6, NULL)"))
            conn.execute(sa.text(
                "INSERT INTO orders VALUES (1, 1, NULL), (2, 2, 5), (3, 3, NULL)"
            ))
            conn.execute(sa.text(
                "INSERT INTO user_services VALUES "
                "(1, 1, 'order:1'), "            # platform order
                "(2, 2, 'order:2'), "            # shop order
                "(3, 2, 'imported'), "           # attributed to the shop, consistent evidence
                "(4, 3, 'order:3'), "            # attributed to the shop but sold by the platform
                "(5, 3, 'imported'), "           # ambiguous: shop attribution, platform order
                "(6, 4, 'order:999'), "          # order missing
                "(7, 4, 'order:abc'), "          # malformed remark
                "(8, 6, 'linked'), "             # no evidence, no attribution
                "(9, 1, 'order:2')"              # order belongs to another customer
            ))

    def run_migration(self, conn, *, down: bool = False):
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def services(self, conn) -> dict[int, tuple[int | None, int]]:
        rows = conn.execute(sa.text("SELECT id, shop_id, shop_needs_review FROM user_services ORDER BY id"))
        return {int(r.id): (r.shop_id, int(r.shop_needs_review)) for r in rows}

    def members(self, conn) -> set[tuple[int, int | None]]:
        rows = conn.execute(sa.text("SELECT bot_user_id, shop_id FROM bot_user_shops"))
        return {(int(r.bot_user_id), r.shop_id) for r in rows}

    def test_every_service_gets_the_shop_that_sold_it_or_goes_to_review(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            self.assertEqual(self.services(conn), {
                1: (None, 0), 2: (SHOP, 0), 3: (SHOP, 0), 4: (None, 0), 5: (None, 1),
                6: (None, 1), 7: (None, 1), 8: (None, 0), 9: (None, 1),
            })

    def test_membership_covers_attribution_orders_and_services(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            members = self.members(conn)
        self.assertIn((1, None), members)
        self.assertIn((2, SHOP), members)
        self.assertEqual({shop for uid, shop in members if uid == 3}, {SHOP, None})
        self.assertIn((6, None), members)
        self.assertNotIn((1, SHOP), members)

    def test_running_it_twice_changes_nothing(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            before = (self.services(conn), self.members(conn))
            self.run_migration(conn)
            self.assertEqual((self.services(conn), self.members(conn)), before)

    def test_downgrade_keeps_services_and_a_later_upgrade_works(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            self.run_migration(conn, down=True)
            inspector = sa.inspect(conn)
            self.assertNotIn("bot_user_shops", inspector.get_table_names())
            self.assertNotIn("shop_id", {c["name"] for c in inspector.get_columns("user_services")})
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM user_services")).scalar(), 9)
            self.run_migration(conn)
            self.assertEqual(self.services(conn)[2], (SHOP, 0))

    def test_large_tables_are_processed_in_batches(self):
        with self.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO bot_users VALUES (100, NULL)"))
            conn.execute(
                sa.text("INSERT INTO user_services (id, bot_user_id, remark) VALUES (:id, 100, 'linked')"),
                [{"id": 1000 + i} for i in range(self.migration.BATCH_SIZE * 2 + 7)],
            )
            self.run_migration(conn)
            total = conn.execute(sa.text("SELECT COUNT(*) FROM user_services WHERE bot_user_id = 100")).scalar()
            review = conn.execute(
                sa.text("SELECT COUNT(*) FROM user_services WHERE bot_user_id = 100 AND shop_needs_review")
            ).scalar()
        self.assertEqual((total, review), (self.migration.BATCH_SIZE * 2 + 7, 0))


if __name__ == "__main__":
    unittest.main()
