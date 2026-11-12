"""Additive random-discount schema for Alembic and legacy SQLite startup."""
from __future__ import annotations

from sqlalchemy import Connection, inspect, text

from app.db.models import RandomDiscount, RandomDiscountDraw, RandomDiscountReservation

_ORDER_COLUMNS = {
    "random_discount_amount": "ALTER TABLE orders ADD COLUMN random_discount_amount INTEGER NOT NULL DEFAULT 0",
    "random_discount_id": "ALTER TABLE orders ADD COLUMN random_discount_id INTEGER REFERENCES random_discounts(id)",
}
_INDEXES = (
    ("orders", "ix_orders_random_discount_id", ("random_discount_id",)),
    ("orders", "ix_orders_shop_amount_created", ("reseller_id", "amount", "created_at")),
    ("payments", "ix_payments_wallet_shop_amount", ("wallet_shop_id", "amount", "created_at")),
)


def upgrade_random_discounts(connection: Connection) -> None:
    RandomDiscount.__table__.create(connection, checkfirst=True)
    inspector = inspect(connection)
    if inspector.has_table("orders"):
        columns = {column["name"] for column in inspector.get_columns("orders")}
        for name, statement in _ORDER_COLUMNS.items():
            if name not in columns:
                connection.execute(text(statement))
    RandomDiscountDraw.__table__.create(connection, checkfirst=True)
    RandomDiscountReservation.__table__.create(connection, checkfirst=True)
    for table, name, columns in _INDEXES:
        if not inspect(connection).has_table(table):
            continue
        existing = {index["name"] for index in inspect(connection).get_indexes(table)}
        if name not in existing:
            # Every identifier comes from the fixed declarations above.
            connection.execute(text(f"CREATE INDEX {name} ON {table} ({', '.join(columns)})"))
