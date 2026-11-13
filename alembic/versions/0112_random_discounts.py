"""Stable shop random discounts, amount reservations and checkout snapshots.

Revision ID: 0112_random_discounts
Revises: 0111_referral_shop

Adds policy/draw/reservation tables and two additive order fields. Existing order
amounts are retained. The downgrade removes the feature schema and preserves orders.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

from app.db.random_discounts_schema import upgrade_random_discounts

revision: str = "0112_random_discounts"
down_revision: str = "0111_referral_shop"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    upgrade_random_discounts(op.get_bind())


@contextmanager
def _checked_sqlite_rebuild(bind: sa.Connection) -> Iterator[None]:
    if bind.dialect.name != "sqlite":
        yield
        return
    if not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql("BEGIN")
    indexes = bind.execute(sa.text(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='orders' "
        "AND name=:index_name AND sql IS NOT NULL LIMIT 1"
    ), {"index_name": "uq_orders_open_requested_username"}).all()
    was_deferred = bool(bind.exec_driver_sql("PRAGMA defer_foreign_keys").scalar())
    bind.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    yield
    for name, definition in indexes:
        present = bind.execute(sa.text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:name"), {"name": name}).first()
        if present is None:
            # Reflection omits expression indexes; their existing schema DDL is trusted.
            bind.exec_driver_sql(definition)
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Random-discount downgrade would leave invalid foreign-key references")
    # A batch rebuild restores the parent table, but SQLite retains the DROP's
    # deferred counters. Clear them only after validation succeeds, then restore its mode.
    bind.exec_driver_sql("PRAGMA defer_foreign_keys=OFF")
    if was_deferred:
        bind.exec_driver_sql("PRAGMA defer_foreign_keys=ON")


def _drop_order_columns(bind: sa.Connection) -> None:
    columns = {column["name"] for column in sa.inspect(bind).get_columns("orders")}
    if not columns.intersection({"random_discount_id", "random_discount_amount"}):
        return
    with _checked_sqlite_rebuild(bind), op.batch_alter_table("orders") as batch:
        for foreign_key in sa.inspect(bind).get_foreign_keys("orders"):
            if foreign_key.get("constrained_columns") == ["random_discount_id"] and foreign_key.get("name"):
                batch.drop_constraint(foreign_key["name"], type_="foreignkey")
        for column in ("random_discount_id", "random_discount_amount"):
            if column in columns:
                batch.drop_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    for table, name in (
        ("orders", "ix_orders_random_discount_id"),
        ("orders", "ix_orders_shop_amount_created"),
        ("payments", "ix_payments_wallet_shop_amount"),
    ):
        inspector = sa.inspect(bind)
        if inspector.has_table(table) and name in {index["name"] for index in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
    for table in ("random_discount_reservations", "random_discount_draws"):
        if sa.inspect(bind).has_table(table):
            op.drop_table(table)
    if sa.inspect(bind).has_table("orders"):
        _drop_order_columns(bind)
    if sa.inspect(bind).has_table("random_discounts"):
        op.drop_table("random_discounts")
