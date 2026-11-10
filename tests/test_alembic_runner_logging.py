"""Running migrations from the app keeps the app's loggers and log setup."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from app.db.alembic_runner import stamp_head


class AlembicRunnerLoggingTests(unittest.TestCase):
    def test_migrations_do_not_disable_existing_loggers_or_reset_handlers(self) -> None:
        existing = logging.getLogger("app.services.example_before_migrations")
        root = logging.getLogger()
        handler = logging.NullHandler()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        with tempfile.TemporaryDirectory() as tmp:
            stamp_head(f"sqlite:///{Path(tmp) / 'm.db'}")  # runs alembic/env.py
        self.assertFalse(existing.disabled)
        self.assertIn(handler, root.handlers)
        with self.assertLogs(existing.name, level="WARNING"):
            existing.warning("still logged")


if __name__ == "__main__":
    unittest.main()
