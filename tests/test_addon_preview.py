"""Addon preview arithmetic and Mini App idempotency migration."""

from __future__ import annotations

import importlib.util
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.services.service_addons import (
    ADDON_PREVIEW_NOTICE,
    addon_terms,
    calculate_addon_preview,
)

GB = 1024**3
DAY = 86400
ROOT = Path(__file__).resolve().parents[1]


class AddonPreviewArithmeticTests(unittest.TestCase):
    def volume(self, *, limit=10 * GB, used=3 * GB, amount=2.5, **extra):
        return calculate_addon_preview(
            {
                "status": "active",
                "data_limit": limit,
                "used_traffic": used,
                "expire": int(time.time()) + 5 * DAY,
                **extra,
            },
            kind="volume",
            amount=amount,
            now=1_700_000_000,
        )

    def duration(self, *, expire=1_700_000_000 + 5 * DAY, amount=7, **extra):
        return calculate_addon_preview(
            {
                "status": "active",
                "data_limit": 10 * GB,
                "used_traffic": 1 * GB,
                "expire": expire,
                **extra,
            },
            kind="duration",
            amount=amount,
            now=1_700_000_000,
        )

    def test_volume_adds_remaining_without_resetting_used(self):
        result = self.volume()
        self.assertEqual(result["result_remaining_bytes"], int(9.5 * GB))
        self.assertTrue(any("حجم پس از افزایش" in line for line in result["lines"]))
        self.assertEqual(result["notice"], ADDON_PREVIEW_NOTICE)

    def test_unlimited_volume_is_rejected(self):
        with self.assertRaises(ValueError):
            self.volume(limit=0)

    def test_duration_extends_from_live_expiry(self):
        result = self.duration()
        self.assertEqual(result["result_seconds"], 12 * DAY)
        self.assertTrue(any("انقضای جدید" in line for line in result["lines"]))

    def test_unlimited_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.duration(expire=0)

    def test_on_hold_duration_extends_pending_timer(self):
        result = self.duration(
            expire=0, status="on_hold", on_hold_expire_duration=5 * DAY,
        )
        self.assertEqual(result["result_seconds"], 12 * DAY)
        self.assertTrue(any("پس از اتصال" in line for line in result["lines"]))

    def test_addon_terms_freeze_pack_entitlement(self):
        volume = SimpleNamespace(kind="volume", amount=2.5)
        duration = SimpleNamespace(kind="duration", amount=7)
        self.assertEqual(
            addon_terms(volume),
            {"version": 1, "kind": "volume", "volume_bytes": int(2.5 * GB), "duration_seconds": 0},
        )
        self.assertEqual(
            addon_terms(duration),
            {"version": 1, "kind": "duration", "volume_bytes": 0, "duration_seconds": 7 * DAY},
        )


def load_migration():
    spec = importlib.util.spec_from_file_location(
        "addon_request_key_migration", ROOT / "alembic/versions/0089_addon_request_key.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AddonRequestKeyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = load_migration()
        self.engine = sa.create_engine("sqlite://")
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE orders (id INTEGER PRIMARY KEY)"))

    def run_step(self, conn: sa.Connection, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def columns(self, conn: sa.Connection) -> set[str]:
        return {c["name"] for c in sa.inspect(conn).get_columns("orders")}

    def indexes(self, conn: sa.Connection) -> set[str]:
        return {i["name"] for i in sa.inspect(conn).get_indexes("orders")}

    def test_upgrade_twice_then_downgrade_keeps_rows(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO orders (id) VALUES (1)"))
            self.run_step(conn)
            self.run_step(conn)
            self.assertIn("addon_request_key", self.columns(conn))
            self.assertIn("uq_orders_addon_request_key", self.indexes(conn))
            self.assertEqual(conn.execute(sa.text("SELECT id FROM orders")).scalar(), 1)
            self.run_step(conn, down=True)
            self.assertNotIn("addon_request_key", self.columns(conn))
            self.assertEqual(conn.execute(sa.text("SELECT id FROM orders")).scalar(), 1)

    def test_there_is_a_single_migration_head(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        self.assertEqual(len(ScriptDirectory.from_config(config).get_heads()), 1)
