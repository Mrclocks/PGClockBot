"""When a payment's receipt arrived.

Revision ID: 0075_payment_receipt_at
Revises: 0074_first_use_nudge

``payments.receipt_at`` records when the latest receipt was attached, so a receipt that nobody
reviewed for a shop-chosen number of minutes can be approved automatically. A partial index keeps
the scan for waiting receipts cheap. Existing rows keep NULL and are never auto-approved. Idempotent;
the downgrade drops the index and the column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0075_payment_receipt_at"
down_revision: Union[str, None] = "0074_first_use_nudge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "payments"
COLUMN = "receipt_at"
INDEX = "ix_payments_pending_receipt"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if COLUMN not in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
    if INDEX not in _indexes(bind):
        where = sa.text(f"status = 'pending' AND {COLUMN} IS NOT NULL")
        op.create_index(INDEX, TABLE, [COLUMN], sqlite_where=where, postgresql_where=where)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if INDEX in _indexes(bind):
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
