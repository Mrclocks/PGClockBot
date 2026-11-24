"""Cashback on wallet top-ups: a grant can belong to a payment instead of an order.

Revision ID: 0124_cashback_topup_grants
Revises: 0123_auto_renew_offer

``cashback_grants.order_id`` becomes nullable and ``payment_id`` is added with a partial unique
index, so each top-up payment earns at most one grant. Idempotent. The downgrade drops the index
and the column and keeps every row; ``order_id`` stays nullable, because top-up grants have none.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0124_cashback_topup_grants"
down_revision: Union[str, None] = "0123_auto_renew_offer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "cashback_grants"
COLUMN = "payment_id"
INDEX = "uq_cashback_grant_payment"
HAS_PAYMENT = sa.text("payment_id IS NOT NULL")


def _columns() -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def upgrade() -> None:
    columns = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if not columns["order_id"]["nullable"]:
            batch.alter_column("order_id", existing_type=sa.Integer(), nullable=True)
        if COLUMN not in columns:
            batch.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
    if INDEX not in _indexes():
        op.create_index(INDEX, TABLE, [COLUMN], unique=True, sqlite_where=HAS_PAYMENT, postgresql_where=HAS_PAYMENT)


def downgrade() -> None:
    if INDEX in _indexes():
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
