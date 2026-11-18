"""Automatic retry state for paid orders whose delivery failed.

Revision ID: 0119_delivery_failure_auto_retry
Revises: 0118_shop_targeted_message_days

Adds ``auto_attempts``, ``next_auto_retry_at`` and ``auto_retry_stopped_at`` to
``delivery_failures`` and a partial index for the retry job. Rows that were already open
when the columns are added are marked as stopped: staff were handling them by hand, so the
job never refunds or re-delivers an old order on deploy.

Idempotent (columns and index are only added when missing); the downgrade drops them and
keeps every failure row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0119_delivery_failure_auto_retry"
down_revision: Union[str, None] = "0118_shop_targeted_message_days"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "delivery_failures"
INDEX = "ix_delivery_failures_auto_retry"
OPEN_AUTO = "resolved_at IS NULL AND auto_retry_stopped_at IS NULL"


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    existing = _columns(bind)
    added_stop_column = "auto_retry_stopped_at" not in existing
    with op.batch_alter_table(TABLE) as batch:
        if "auto_attempts" not in existing:
            batch.add_column(sa.Column("auto_attempts", sa.Integer(), nullable=False, server_default="0"))
        if "next_auto_retry_at" not in existing:
            batch.add_column(sa.Column("next_auto_retry_at", sa.DateTime(timezone=True), nullable=True))
        if added_stop_column:
            batch.add_column(sa.Column("auto_retry_stopped_at", sa.DateTime(timezone=True), nullable=True))
    if added_stop_column:
        op.execute(
            sa.text(f"UPDATE {TABLE} SET auto_retry_stopped_at = CURRENT_TIMESTAMP WHERE resolved_at IS NULL")
        )
    if INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(
            INDEX, TABLE, ["next_auto_retry_at"],
            sqlite_where=sa.text(OPEN_AUTO), postgresql_where=sa.text(OPEN_AUTO),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    existing = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for name in ("auto_retry_stopped_at", "next_auto_retry_at", "auto_attempts"):
            if name in existing:
                batch.drop_column(name)
