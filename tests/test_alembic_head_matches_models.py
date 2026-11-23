"""Alembic head builds every table and column the models declare.

Production databases get their schema only from Alembic, so a model column
without a migration would fail at runtime on every install.
"""

from __future__ import annotations

import unittest

from sqlalchemy import inspect

from app.db import Base
from app.db.alembic_runner import upgrade_head
import app.db.models  # noqa: F401  (registers every table on Base.metadata)
from tests.postgres_support import scoped_database_url, scoped_sync_engine


class AlembicHeadMatchesModelsTests(unittest.TestCase):
    def test_every_model_table_and_column_exists_after_upgrade(self) -> None:
        upgrade_head(scoped_database_url(tables=False))
        engine = scoped_sync_engine()
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            missing_tables = sorted(set(Base.metadata.tables) - tables)
            missing_columns = sorted(
                f"{table.name}.{column.name}"
                for table in Base.metadata.tables.values()
                if table.name in tables
                for column in table.columns
                if column.name not in {c["name"] for c in inspector.get_columns(table.name)}
            )
        finally:
            engine.dispose()
        self.assertEqual(missing_tables, [])
        self.assertEqual(missing_columns, [])
