"""``customer_services`` gates shop bot service info/edit; migration grants it with dashboard."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa

from app.bot.handlers.reseller_customer_services import PERMISSION, may_manage_customer_services
from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS, parse_perms

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0084_customer_services_permission.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0084", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CustomerServicesKeyTests(unittest.TestCase):
    def test_key_is_a_shop_permission_and_a_default(self) -> None:
        self.assertEqual(PERMISSION, "customer_services")
        self.assertIn(PERMISSION, {key for key, _ in FEATURE_PERMS})
        self.assertIn(PERMISSION, parse_perms(DEFAULT_FEATURE_PERMS))

    def test_may_manage_requires_the_key(self) -> None:
        from types import SimpleNamespace

        yes = SimpleNamespace(is_active=True, web_permissions="dashboard,customer_services")
        no = SimpleNamespace(is_active=True, web_permissions="dashboard,tickets")
        self.assertTrue(may_manage_customer_services(yes))
        self.assertFalse(may_manage_customer_services(no))
        self.assertFalse(may_manage_customer_services(None))


class CustomerServicesMigrationTests(unittest.TestCase):
    ROWS = {
        1: "dashboard,orders",
        2: "tickets",
        3: None,
        4: "",
        5: "dashboard,customer_services",
    }

    def _setup(self, conn) -> None:
        for table in ("reseller_profiles", "reseller_plans"):
            conn.execute(
                sa.text(
                    f"CREATE TABLE {table} ("
                    "id INTEGER PRIMARY KEY, web_permissions TEXT, bot_permissions TEXT)"
                )
            )
            for row_id, raw in self.ROWS.items():
                conn.execute(
                    sa.text(
                        f"INSERT INTO {table} (id, web_permissions, bot_permissions) "
                        "VALUES (:id, :w, :b)"
                    ),
                    {"id": row_id, "w": raw, "b": raw},
                )

    def _rows(self, conn, table: str) -> dict:
        return {
            r.id: (r.web_permissions, r.bot_permissions)
            for r in conn.execute(sa.text(f"SELECT * FROM {table}"))
        }

    def test_upgrade_twice_then_downgrade(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            self._setup(conn)
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                expected = {
                    1: "dashboard,orders,customer_services",
                    2: "tickets",
                    3: None,
                    4: "",
                    5: "dashboard,customer_services",
                }
                for table in ("reseller_profiles", "reseller_plans"):
                    self.assertEqual(
                        self._rows(conn, table), {k: (v, v) for k, v in expected.items()}
                    )
                migration.downgrade()
                after = self._rows(conn, "reseller_profiles")
                self.assertEqual(after[1], ("dashboard,orders", "dashboard,orders"))
                self.assertEqual(after[5], ("dashboard", "dashboard"))
                self.assertEqual(after[3], (None, None))
                self.assertEqual(after[4], ("", ""))
        engine.dispose()
