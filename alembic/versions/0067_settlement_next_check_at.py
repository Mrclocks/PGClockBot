"""Schedule PSP re-verification per settlement, so abandoned checkouts back off.

Revision ID: 0067_settlement_next_check_at
Revises: 0066_external_volume_allocation

The PSP poller used to pick the oldest ``updated_at`` rows, which stopped changing once a
verify kept failing with the same message; abandoned checkouts then filled every batch and
new payments were never re-verified. ``next_check_at`` is set on every check. Existing rows
start NULL (due now). Idempotent; the downgrade drops the column and index and keeps rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0067_settlement_next_check_at"
down_revision: Union[str, None] = "0066_external_volume_allocation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "payment_settlements"
COLUMN = "next_check_at"
INDEX = "ix_payment_settlements_next_check_at"


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
        op.create_index(INDEX, TABLE, [COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if INDEX in _indexes(bind):
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
