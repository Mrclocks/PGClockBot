"""0094 adds targeted_campaigns.request_key; the key makes campaign creation repeat-safe."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]


def _load_migration():
    path = ROOT / "alembic/versions/0094_campaign_request_key.py"
    spec = importlib.util.spec_from_file_location("campaign_request_key_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CampaignRequestKeyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = _load_migration()
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE targeted_campaigns (id INTEGER PRIMARY KEY, text TEXT)"))
            conn.execute(sa.text("INSERT INTO targeted_campaigns (id, text) VALUES (1, 'old')"))

    def _run(self, conn, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def test_upgrade_twice_and_downgrade_keep_rows_and_make_keys_unique(self) -> None:
        with self.engine.begin() as conn:
            self._run(conn)
            self._run(conn)
            self.assertIn("request_key", {c["name"] for c in sa.inspect(conn).get_columns("targeted_campaigns")})
            conn.execute(sa.text("INSERT INTO targeted_campaigns (id, text, request_key) VALUES (2, 'a', 'k')"))
            conn.execute(sa.text("INSERT INTO targeted_campaigns (id, text) VALUES (3, 'no key')"))
            conn.execute(sa.text("INSERT INTO targeted_campaigns (id, text) VALUES (4, 'no key either')"))
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(sa.text("INSERT INTO targeted_campaigns (id, text, request_key) VALUES (5, 'b', 'k')"))
        with self.engine.begin() as conn:
            self._run(conn, down=True)
            self.assertNotIn("request_key", {c["name"] for c in sa.inspect(conn).get_columns("targeted_campaigns")})
            self.assertEqual(conn.execute(sa.text("SELECT text FROM targeted_campaigns WHERE id=1")).scalar(), "old")
            self._run(conn)


if __name__ == "__main__":
    unittest.main()
