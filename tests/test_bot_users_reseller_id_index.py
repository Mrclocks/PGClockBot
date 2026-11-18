"""``bot_users.reseller_id`` — filtered by every reseller-scoped query (tenant
isolation: bot's "my customers" list, dashboards, finance) — must be indexed,
both in the ORM model and via an Alembic migration so existing databases
pick it up on upgrade.
"""

from __future__ import annotations

import unittest
from tests.postgres_support import scoped_database_url, scoped_sync_engine


class BotUsersResellerIdIndexTests(unittest.TestCase):
    def test_model_column_is_indexed(self):
        from app.db.models import BotUser

        col = BotUser.__table__.c.reseller_id
        self.assertTrue(
            col.index, "bot_users.reseller_id must be indexed (tenant-isolation queries filter on it)"
        )

    def test_migration_creates_index(self):
        from sqlalchemy import inspect

        from app.db.alembic_runner import upgrade_head

        upgrade_head(scoped_database_url(tables=False))
        engine = scoped_sync_engine()
        try:
            names = {ix["name"] for ix in inspect(engine).get_indexes("bot_users")}
            self.assertIn("ix_bot_users_reseller_id", names)
        finally:
            engine.dispose()

    def test_ensure_indexes_statement_present(self):
        """Legacy pre-Alembic SQLite bootstrap path must also get this index."""
        from pathlib import Path

        src = Path("app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("ix_bot_users_reseller_id ON bot_users (reseller_id)", src)


if __name__ == "__main__":
    unittest.main()
