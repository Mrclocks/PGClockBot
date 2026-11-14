"""0093 adds wallet_transactions.idempotency_key for admin adjust replay safety."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]


def _load_migration():
    path = ROOT / "alembic/versions/0093_wallet_tx_idempotency.py"
    spec = importlib.util.spec_from_file_location("wallet_tx_idempotency_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class WalletTxIdempotencyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = _load_migration()
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE wallet_transactions ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER, "
                    "balance_after INTEGER, reason TEXT, reseller_id INTEGER, "
                    "created_at TEXT)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO wallet_transactions "
                    "(id, user_id, amount, balance_after, reason, reseller_id) "
                    "VALUES (1, 9, 1000, 1000, 'seed', NULL)"
                )
            )

    def _run(self, conn, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def test_upgrade_twice_and_downgrade_preserve_rows(self) -> None:
        with self.engine.begin() as conn:
            self._run(conn)
            cols = {c["name"] for c in sa.inspect(conn).get_columns("wallet_transactions")}
            self.assertIn("idempotency_key", cols)
            indexes = {i["name"] for i in sa.inspect(conn).get_indexes("wallet_transactions")}
            self.assertIn("uq_wallet_tx_idempotency", indexes)
            self._run(conn)  # idempotent second upgrade
            self.assertEqual(
                conn.execute(sa.text("SELECT amount, reason FROM wallet_transactions WHERE id=1")).one(),
                (1000, "seed"),
            )
            self._run(conn, down=True)
            cols_after = {c["name"] for c in sa.inspect(conn).get_columns("wallet_transactions")}
            self.assertNotIn("idempotency_key", cols_after)
            self.assertEqual(
                conn.execute(sa.text("SELECT amount FROM wallet_transactions WHERE id=1")).scalar(),
                1000,
            )
            self._run(conn)
            self.assertIn(
                "idempotency_key",
                {c["name"] for c in sa.inspect(conn).get_columns("wallet_transactions")},
            )


if __name__ == "__main__":
    unittest.main()
