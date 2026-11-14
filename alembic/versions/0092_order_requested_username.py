"""Orders keep the service name the buyer chose at checkout.

Revision ID: 0092_order_requested_username
Revises: 0091_plan_customer_extra_rates

``orders`` gains a nullable, indexed ``requested_username``, unique per shop among open orders. Delivery puts it in place of the
naming pattern's ``{random}`` part. Existing orders keep NULL (generated names).
Idempotent; the downgrade drops the column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0092_order_requested_username"
down_revision: Union[str, None] = "0091_plan_customer_extra_rates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "orders"
COLUMN = "requested_username"
# The name step looks up open orders holding a name; without it each lookup scans orders.
INDEX = "ix_orders_requested_username"
# One open order per shop may hold a name, so two buyers cannot store the same one at once.
UNIQUE_INDEX = "uq_orders_open_requested_username"
OPEN_NAMED_ORDER_SQL = (
    "requested_username IS NOT NULL AND status IN "
    "('pending', 'rejected', 'awaiting_receipt', 'paid', 'delivering')"
)


def _has_column(bind: sa.Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return False
    return COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}


def _has_index(bind: sa.Connection, name: str = INDEX) -> bool:
    if bind.dialect.name == "sqlite":
        # SQLAlchemy cannot reflect SQLite expression indexes; the table pragma lists them.
        return any(row._mapping["name"] == name for row in bind.execute(sa.text(f"PRAGMA index_list('{TABLE}')")))
    return name in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if not _has_column(bind):
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(32), nullable=True))
    if not _has_index(bind):
        op.create_index(INDEX, TABLE, [COLUMN])
    if not _has_index(bind, UNIQUE_INDEX):
        op.create_index(
            UNIQUE_INDEX,
            TABLE,
            [sa.text("coalesce(reseller_id, 0)"), COLUMN],
            unique=True,
            sqlite_where=sa.text(OPEN_NAMED_ORDER_SQL),
            postgresql_where=sa.text(OPEN_NAMED_ORDER_SQL),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        return
    if _has_index(bind, UNIQUE_INDEX):
        op.drop_index(UNIQUE_INDEX, table_name=TABLE)
    if _has_index(bind):
        op.drop_index(INDEX, table_name=TABLE)
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
