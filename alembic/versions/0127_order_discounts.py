"""Order discount ledger: one line per reduction source on an order.

Revision ID: 0127_order_discounts
Revises: 0126_service_transfers

Adds ``order_discounts`` with a unique (order, source) pair so a source is recorded once per
order, plus the indexes per-shop reports and per-customer limits read. Existing orders keep
their ``discount_amount``/``discount_code``; lines start with new orders. Idempotent; the
downgrade drops the table and changes no order.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0127_order_discounts"
down_revision: Union[str, None] = "0126_service_transfers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "order_discounts"
INDEXES = {
    "ix_order_discounts_shop_source_ref": ["shop_key", "source", "source_ref"],
    "ix_order_discounts_shop_user": ["shop_key", "user_id"],
}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("shop_key", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("source_ref", sa.String(64), nullable=True),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("order_id", "source", name="uq_order_discounts_order_source"),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    for name, columns in INDEXES.items():
        if name not in existing:
            op.create_index(name, TABLE, columns)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
