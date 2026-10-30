"""Durable broadcast queue.

Revision ID: 0072_broadcast_queue
Revises: 0071_settlement_psp_gateway

A broadcast is queued and sent by a background worker that keeps a cursor, a lease and the user
currently being sent to. Existing history rows are marked ``done``. Idempotent; the downgrade drops
only the new columns and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0072_broadcast_queue"
down_revision: Union[str, None] = "0071_settlement_psp_gateway"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "broadcast_logs"
INDEX = "ix_broadcast_logs_status"
COLUMNS = (
    ("status", lambda: sa.Column("status", sa.String(16), nullable=False, server_default="done")),
    ("cursor_id", lambda: sa.Column("cursor_id", sa.Integer(), nullable=False, server_default="0")),
    ("inflight_id", lambda: sa.Column("inflight_id", sa.Integer(), nullable=True)),
    ("review_count", lambda: sa.Column("review_count", sa.Integer(), nullable=False, server_default="0")),
    ("lock_token", lambda: sa.Column("lock_token", sa.String(32), nullable=True)),
    ("locked_until", lambda: sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True)),
    ("completed_at", lambda: sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)),
)


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for name, make in COLUMNS:
            if name not in present:
                batch.add_column(make())
    if INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if INDEX in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    present = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for name, _ in COLUMNS:
            if name in present:
                batch.drop_column(name)
