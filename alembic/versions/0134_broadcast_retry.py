"""Resend a group message to the recipients it did not reach.

Revision ID: 0134_broadcast_retry
Revises: 0133_broadcast_failures

Adds ``broadcast_logs.retry_of_id`` (the broadcast a resend belongs to, a plain id) with a partial
unique index so a broadcast is resent at most once, and ``broadcast_logs.retry_unknown`` (NOT NULL,
default false). Existing broadcasts are not resends. Idempotent; the downgrade drops the index and
both columns and keeps every broadcast.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0134_broadcast_retry"
down_revision: Union[str, None] = "0133_broadcast_failures"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "broadcast_logs"
RETRY_INDEX = "uq_broadcast_logs_retry_of_id"


def _columns(bind: sa.engine.Connection) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if "retry_of_id" not in _columns(bind):
        op.add_column(TABLE, sa.Column("retry_of_id", sa.Integer(), nullable=True))
    if "retry_unknown" not in _columns(bind):
        op.add_column(TABLE, sa.Column("retry_unknown", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    if RETRY_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(
            RETRY_INDEX, TABLE, ["retry_of_id"], unique=True, postgresql_where=sa.text("retry_of_id IS NOT NULL")
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if RETRY_INDEX in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.drop_index(RETRY_INDEX, table_name=TABLE)
    for name in ("retry_unknown", "retry_of_id"):
        if name in _columns(bind):
            op.drop_column(TABLE, name)
