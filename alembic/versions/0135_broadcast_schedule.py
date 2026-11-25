"""Scheduled group messages.

Revision ID: 0135_broadcast_schedule
Revises: 0134_broadcast_retry

Adds nullable ``broadcast_logs.send_after``: the time a queued broadcast may start sending.
Existing broadcasts keep NULL (send as soon as possible). Idempotent; the downgrade drops the
column and keeps every broadcast.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0135_broadcast_schedule"
down_revision: Union[str, None] = "0134_broadcast_retry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "broadcast_logs"
COLUMN = "send_after"


def _has_column(bind: sa.engine.Connection) -> bool | None:
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return None
    return COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if _has_column(op.get_bind()) is False:
        op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column(op.get_bind()):
        op.drop_column(TABLE, COLUMN)
