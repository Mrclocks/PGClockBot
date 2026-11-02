"""Voiding of approved payments.

Revision ID: 0072_payment_voids
Revises: 0071_settlement_psp_gateway

One row per payment an operator voided after approval: who, why, the services that had to be
stopped and whether the refund to the shop wallet was completed. Idempotent; the downgrade
drops only this table and leaves payments, orders and services untouched.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0072_payment_voids"
down_revision: Union[str, None] = "0071_settlement_psp_gateway"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "payment_voids"
INDEXES = {
    "ix_payment_voids_shop_id": ["shop_id"],
    "ix_payment_voids_status": ["status"],
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("shop_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=True),
            sa.Column("actor", sa.String(128), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("refund_amount", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="processing"),
            sa.Column("service_ids", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("lock_token", sa.String(32), nullable=True),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("payment_id", name="uq_payment_voids_payment_id"),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    for name, columns in INDEXES.items():
        if name not in existing:
            op.create_index(name, TABLE, columns)


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
