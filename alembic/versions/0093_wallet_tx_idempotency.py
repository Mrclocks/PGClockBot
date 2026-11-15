"""Idempotent keys for manual platform wallet ledger rows.

Revision ID: 0093_wallet_tx_idempotency
Revises: 0092_order_requested_username

Adds nullable ``idempotency_key`` on ``wallet_transactions`` so a repeated admin
wallet credit/debit (same form nonce / FSM key) inserts at most one ledger row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0093_wallet_tx_idempotency"
down_revision: Union[str, None] = "0092_order_requested_username"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "wallet_transactions"
COLUMN = "idempotency_key"
INDEX = "uq_wallet_tx_idempotency"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if TABLE not in tables:
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    if COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(128), nullable=True))
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}
    if INDEX not in indexes:
        # NULL keys stay non-unique (legacy rows); only concrete keys collide.
        op.create_index(
            INDEX,
            TABLE,
            [COLUMN],
            unique=True,
            sqlite_where=sa.text(f"{COLUMN} IS NOT NULL"),
            postgresql_where=sa.text(f"{COLUMN} IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if TABLE not in tables:
        return
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}
    if INDEX in indexes:
        op.drop_index(INDEX, table_name=TABLE)
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
