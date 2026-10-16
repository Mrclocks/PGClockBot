"""Keep the lucky-wheel counters of a customer per shop.

Revision ID: 0062_wheel_state_per_shop
Revises: 0061_shop_points

``lucky_wheel_user_state`` was unique per customer, so free spins, the daily limit and the
cooldown earned in one shop's wheel were spent or consumed in every other wheel. It becomes
unique per (customer, shop); the platform row has no shop (two partial unique indexes keep
one row per pair on SQLite and PostgreSQL). Existing rows keep their shop.

The downgrade restores the old unique constraint; the counters are cheap state, so when a
customer has several rows only the newest one is kept.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0062_wheel_state_per_shop"
down_revision: Union[str, None] = "0061_shop_points"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "lucky_wheel_user_state"
OLD_UNIQUE = "uq_lucky_wheel_user_state_user"
SHOP_UNIQUE = "uq_lucky_wheel_state_shop"
PLATFORM_UNIQUE = "uq_lucky_wheel_state_platform"


def _unique_names(inspector: sa.Inspector) -> set[str]:
    return {u["name"] for u in inspector.get_unique_constraints(TABLE) if u.get("name")}


def _per_user_unique_names(inspector: sa.Inspector) -> list[str]:
    """Named unique constraints on exactly (user_id), whatever created them (the 0023 name or a database default)."""
    return [
        u["name"] for u in inspector.get_unique_constraints(TABLE)
        if u.get("name") and list(u.get("column_names") or []) == ["user_id"]
    ]


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(TABLE):
        return
    stale = _per_user_unique_names(inspector)
    if stale:
        with op.batch_alter_table(TABLE) as batch:
            for name in stale:
                batch.drop_constraint(name, type_="unique")
    indexes = {i["name"] for i in sa.inspect(connection).get_indexes(TABLE)}
    if SHOP_UNIQUE not in indexes:
        op.create_index(
            SHOP_UNIQUE, TABLE, ["user_id", "reseller_id"], unique=True,
            sqlite_where=sa.text("reseller_id IS NOT NULL"), postgresql_where=sa.text("reseller_id IS NOT NULL"),
        )
    if PLATFORM_UNIQUE not in indexes:
        op.create_index(
            PLATFORM_UNIQUE, TABLE, ["user_id"], unique=True,
            sqlite_where=sa.text("reseller_id IS NULL"), postgresql_where=sa.text("reseller_id IS NULL"),
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(TABLE):
        return
    indexes = {i["name"] for i in inspector.get_indexes(TABLE)}
    for name in (SHOP_UNIQUE, PLATFORM_UNIQUE):
        if name in indexes:
            op.drop_index(name, table_name=TABLE)
    connection.execute(
        sa.text(f"DELETE FROM {TABLE} WHERE id NOT IN (SELECT MAX(id) FROM {TABLE} GROUP BY user_id)")  # fixed table name
    )
    if OLD_UNIQUE not in _unique_names(sa.inspect(connection)):
        with op.batch_alter_table(TABLE) as batch:
            batch.create_unique_constraint(OLD_UNIQUE, ["user_id"])
