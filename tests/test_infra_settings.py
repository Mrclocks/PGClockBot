"""Infrastructure knobs: the PostgreSQL pool size comes from .env with safe bounds, GZip runs at level 6."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from starlette.middleware.gzip import GZipMiddleware

from app.config import DB_MAX_OVERFLOW_DEFAULT, DB_POOL_LIMIT, DB_POOL_SIZE_DEFAULT, Settings
from app.db.session import postgres_pool_options


def _settings(**env: str) -> Settings:
    clean = {k: v for k, v in os.environ.items() if k not in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW")}
    with patch.dict(os.environ, {**clean, **env}, clear=True):
        return Settings(_env_file=None)


class DatabasePoolSettingsTests(unittest.TestCase):
    def test_defaults_leave_room_for_a_dashboard_and_the_bots(self) -> None:
        config = _settings()
        self.assertEqual((config.db_pool_size, config.db_max_overflow), (DB_POOL_SIZE_DEFAULT, DB_MAX_OVERFLOW_DEFAULT))
        self.assertEqual(postgres_pool_options(config), {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20})

    def test_values_come_from_the_environment(self) -> None:
        config = _settings(DB_POOL_SIZE="15", DB_MAX_OVERFLOW=" 5 ")
        self.assertEqual(postgres_pool_options(config)["pool_size"], 15)
        self.assertEqual(postgres_pool_options(config)["max_overflow"], 5)

    def test_a_typo_falls_back_to_the_default_instead_of_stopping_the_panel(self) -> None:
        config = _settings(DB_POOL_SIZE="ten", DB_MAX_OVERFLOW="true")
        self.assertEqual((config.db_pool_size, config.db_max_overflow), (DB_POOL_SIZE_DEFAULT, DB_MAX_OVERFLOW_DEFAULT))

    def test_out_of_range_values_are_clamped(self) -> None:
        high = _settings(DB_POOL_SIZE="5000", DB_MAX_OVERFLOW="900")
        self.assertEqual((high.db_pool_size, high.db_max_overflow), (DB_POOL_LIMIT, DB_POOL_LIMIT))
        low = _settings(DB_POOL_SIZE="0", DB_MAX_OVERFLOW="-3")
        self.assertEqual((low.db_pool_size, low.db_max_overflow), (1, 0))


class GzipLevelTests(unittest.TestCase):
    def test_responses_are_compressed_at_level_six(self) -> None:
        from app.api.app import create_api_app

        app = create_api_app()
        gzip = [m for m in app.user_middleware if m.cls is GZipMiddleware]
        self.assertEqual(len(gzip), 1)
        self.assertEqual(gzip[0].kwargs.get("compresslevel"), 6)
        self.assertEqual(gzip[0].kwargs.get("minimum_size"), 400)


if __name__ == "__main__":
    unittest.main()
