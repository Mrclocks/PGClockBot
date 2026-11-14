"""Random-discount upgrades preserve existing checkout rows on both database engines."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.random_discounts_schema import upgrade_random_discounts
from app.db.models import Base, BotUser, Order

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("PGCLOCK_TEST_POSTGRES_URL", "")


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("random_discount_migration", ROOT / "alembic/versions/0112_random_discounts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _existing_schema(connection: sa.Connection) -> None:
    metadata = sa.MetaData()
    users = sa.Table("bot_users", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    orders = sa.Table(
        "orders", metadata, sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer()), sa.Column("reseller_id", sa.Integer()),
        sa.Column("amount", sa.Integer()), sa.Column("discount_amount", sa.Integer()),
        sa.Column("status", sa.String(32)), sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    sa.Table(
        "payments", metadata, sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.ForeignKey("orders.id")),
        sa.Column("user_id", sa.Integer()), sa.Column("method", sa.String(32)),
        sa.Column("status", sa.String(32)), sa.Column("is_wallet_topup", sa.Boolean()),
        sa.Column("wallet_shop_id", sa.Integer()), sa.Column("amount", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    metadata.create_all(connection)
    connection.execute(users.insert().values(id=1))
    connection.execute(orders.insert().values(id=1, user_id=1, amount=1000, discount_amount=0, status="pending"))


def _upgrade(connection: sa.Connection, *, down: bool = False) -> None:
    migration = _migration()
    with Operations.context(MigrationContext.configure(connection)):
        (migration.downgrade if down else migration.upgrade)()


def _populate_feature(connection: sa.Connection) -> None:
    connection.execute(sa.text(
        "INSERT INTO random_discounts "
        "(id, shop_key, title, mode, reduction_kind, minimum, maximum, step, window_minutes, request_key) "
        "VALUES (1, 0, 'Surprise', 'automatic', 'amount', 10, 30, 1, 60, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
    ))
    connection.execute(sa.text(
        "UPDATE orders SET amount=975, discount_amount=25, random_discount_amount=25, random_discount_id=1 WHERE id=1"
    ))
    connection.execute(sa.text(
        "INSERT INTO payments (id, order_id, user_id, amount, method, status, is_wallet_topup) "
        "VALUES (1, 1, 1, 975, 'card', 'pending', false)"
    ))
    connection.execute(sa.text(
        "INSERT INTO random_discount_draws "
        "(shop_key, policy_id, order_id, user_id, base_amount, discount_amount, final_amount, protected_until) "
        "VALUES (0, 1, 1, 1, 1000, 25, 975, '2026-11-12 00:00:00+00:00')"
    ))
    connection.execute(sa.text(
        "INSERT INTO random_discount_reservations (shop_key, final_amount, order_id, protected_until) "
        "VALUES (0, 975, 1, '2026-11-12 00:00:00+00:00')"
    ))


class RandomDiscountMigrationTests(unittest.TestCase):
    def test_populated_metadata_schema_downgrade_preserves_constraints_and_commits(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            Base.metadata.create_all(connection)
            connection.execute(BotUser.__table__.insert().values(id=1, telegram_id=1, referral_code="migration"))
            connection.execute(Order.__table__.insert().values(
                id=1, user_id=1, amount=1000, discount_amount=0, requested_username="migration_name",
            ))
            _populate_feature(connection)
            connection.execute(sa.text("PRAGMA defer_foreign_keys=ON"))
            _upgrade(connection, down=True)
            self.assertEqual(connection.execute(sa.text("PRAGMA foreign_keys")).scalar(), 1)
            self.assertEqual(connection.execute(sa.text("PRAGMA defer_foreign_keys")).scalar(), 1)
            self.assertIsNone(connection.execute(sa.text("PRAGMA foreign_key_check")).first())
            index = connection.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_orders_open_requested_username'"
            )).scalar()
            self.assertEqual(index, "uq_orders_open_requested_username")
        with engine.begin() as connection:
            self.assertEqual(connection.execute(sa.text("SELECT id, amount, discount_amount FROM orders")).one(), (1, 975, 25))
            self.assertEqual(connection.execute(sa.text("SELECT id, order_id, amount FROM payments")).one(), (1, 1, 975))
            with self.assertRaises(sa.exc.IntegrityError), connection.begin_nested():
                connection.execute(sa.text(
                    "INSERT INTO orders (id, user_id, amount, discount_amount, quantity, status, requested_username) "
                    "VALUES (2, 1, 1000, 0, 1, 'pending', 'migration_name')"
                ))
            _upgrade(connection, down=True)
            _upgrade(connection)
            self.assertEqual(connection.execute(sa.text("SELECT amount, random_discount_amount FROM orders")).one(), (975, 0))
            self.assertIsNone(connection.execute(sa.text("PRAGMA foreign_key_check")).first())
        engine.dispose()

    def test_failed_downgrade_keeps_invalid_deferred_references_uncommittable(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            _existing_schema(connection)
            _upgrade(connection)
            _populate_feature(connection)
            connection.commit()
            connection.execute(sa.text("PRAGMA defer_foreign_keys=ON"))
            connection.execute(sa.text("UPDATE payments SET order_id=999 WHERE id=1"))
            with self.assertRaises(RuntimeError):
                _upgrade(connection, down=True)
            self.assertEqual(connection.execute(sa.text("PRAGMA foreign_keys")).scalar(), 1)
            self.assertEqual(connection.execute(sa.text("PRAGMA defer_foreign_keys")).scalar(), 1)
            with self.assertRaises(sa.exc.IntegrityError):
                connection.commit()
            connection.rollback()
        engine.dispose()

    def test_populated_downgrade_preserves_orders_payments_and_foreign_keys(self) -> None:
        with sa.create_engine("sqlite://").begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            _existing_schema(connection)
            _upgrade(connection)
            _populate_feature(connection)
            _upgrade(connection, down=True)
            self.assertEqual(connection.execute(sa.text("SELECT id, amount, discount_amount FROM orders")).one(), (1, 975, 25))
            self.assertEqual(connection.execute(sa.text("SELECT id, order_id, amount FROM payments")).one(), (1, 1, 975))
            self.assertEqual(connection.execute(sa.text("PRAGMA foreign_keys")).scalar(), 1)
            self.assertIsNone(connection.execute(sa.text("PRAGMA foreign_key_check")).first())

    def test_upgrade_twice_and_downgrade_preserve_checkout_rows(self) -> None:
        with sa.create_engine("sqlite://").begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            _existing_schema(connection)
            _upgrade(connection)
            _upgrade(connection)
            self.assertEqual(connection.execute(sa.text(
                "SELECT amount, discount_amount, random_discount_amount, random_discount_id FROM orders WHERE id=1"
            )).one(), (1000, 0, 0, None))
            self.assertTrue(sa.inspect(connection).has_table("random_discount_draws"))
            _upgrade(connection, down=True)
            self.assertEqual(connection.execute(sa.text("SELECT id, amount FROM orders")).one(), (1, 1000))
            self.assertNotIn("random_discount_id", {column["name"] for column in sa.inspect(connection).get_columns("orders")})
            self.assertFalse(sa.inspect(connection).has_table("random_discounts"))
            _upgrade(connection, down=True)

    def test_legacy_upgrade_is_repeatable(self) -> None:
        with sa.create_engine("sqlite://").begin() as connection:
            _existing_schema(connection)
            upgrade_random_discounts(connection)
            upgrade_random_discounts(connection)
            self.assertEqual(connection.execute(sa.text("SELECT COUNT(*) FROM orders")).scalar(), 1)
            names = {index["name"] for index in sa.inspect(connection).get_indexes("orders")}
            self.assertIn("ix_orders_shop_amount_created", names)

    def test_existing_amount_lookup_uses_shop_amount_index(self) -> None:
        with sa.create_engine("sqlite://").begin() as connection:
            _existing_schema(connection)
            _upgrade(connection)
            plan = connection.execute(sa.text(
                "EXPLAIN QUERY PLAN SELECT amount FROM orders "
                "WHERE reseller_id IS NULL AND amount BETWEEN 900 AND 990 "
                "AND (created_at >= '2026-01-01' OR status IN ('pending','paid'))"
            )).all()
            self.assertTrue(any("ix_orders_shop_amount_created" in row[3] for row in plan), plan)


@unittest.skipUnless(POSTGRES_URL, "set PGCLOCK_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class RandomDiscountPostgresMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_preserve_checkout_rows(self) -> None:
        schema = "random_migration_" + uuid4().hex[:12]
        engine = sa.create_engine(POSTGRES_URL)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
                _existing_schema(connection)
                _upgrade(connection)
                _upgrade(connection)
                self.assertEqual(connection.execute(sa.text("SELECT amount, random_discount_amount FROM orders")).one(), (1000, 0))
                _populate_feature(connection)
                _upgrade(connection, down=True)
                self.assertEqual(connection.execute(sa.text("SELECT id, amount FROM orders")).one(), (1, 975))
                self.assertEqual(connection.execute(sa.text("SELECT id, order_id, amount FROM payments")).one(), (1, 1, 975))
                _upgrade(connection, down=True)
                connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            engine.dispose()
