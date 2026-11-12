"""0112 creates the table of customers who dismissed the home offer; it is safe to repeat."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
TABLE = "service_import_offer_dismissals"


def _load_migration():
    path = ROOT / "alembic/versions/0112_service_import_offer_dismissal.py"
    spec = importlib.util.spec_from_file_location("service_import_offer_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ServiceImportOfferMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = _load_migration()
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))

    def _run(self, conn, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def test_upgrade_twice_then_downgrade_and_upgrade_again(self) -> None:
        with self.engine.begin() as conn:
            self._run(conn)
            self._run(conn)
            self.assertTrue(sa.inspect(conn).has_table(TABLE))
            conn.execute(sa.text("INSERT INTO bot_users (id) VALUES (1)"))
            conn.execute(sa.text(f"INSERT INTO {TABLE} (user_id, shop_key) VALUES (1, 0)"))
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(sa.text(f"INSERT INTO {TABLE} (user_id, shop_key) VALUES (1, 0)"))
        with self.engine.begin() as conn:
            self._run(conn, down=True)
            self._run(conn, down=True)
            self.assertFalse(sa.inspect(conn).has_table(TABLE))
            self._run(conn)
            self.assertTrue(sa.inspect(conn).has_table(TABLE))

    def test_revision_follows_the_current_head(self) -> None:
        self.assertEqual(self.migration.down_revision, "0111_referral_shop")


if __name__ == "__main__":
    unittest.main()
