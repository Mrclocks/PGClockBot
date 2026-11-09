"""Columns for the expired and volume-finished service cleanup.

Revision ID: 0098_expired_service_cleanup
Revises: 0097_customer_notice_queue

Adds to ``user_services``: ``cleanup_exempt``, ``cleanup_hold_until``, ``cleanup_warn_stage``,
``cleanup_claimed_at`` and ``volume_exhausted_at``, an index on ``quota_expire_at`` for the
candidate scan and a partial index on ``volume_exhausted_at``. Idempotent; the downgrade drops
only the new columns and indexes and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0098_expired_service_cleanup"
down_revision: Union[str, None] = "0097_customer_notice_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_services"
EXPIRE_INDEX = "ix_user_services_quota_expire_at"
VOLUME_INDEX = "ix_user_services_volume_exhausted"
COLUMNS = ("cleanup_exempt", "cleanup_hold_until", "cleanup_warn_stage", "cleanup_claimed_at", "volume_exhausted_at")


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        if "cleanup_exempt" not in present:
            batch.add_column(sa.Column("cleanup_exempt", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "cleanup_hold_until" not in present:
            batch.add_column(sa.Column("cleanup_hold_until", sa.DateTime(timezone=True), nullable=True))
        if "cleanup_warn_stage" not in present:
            batch.add_column(sa.Column("cleanup_warn_stage", sa.Integer(), nullable=False, server_default="0"))
        if "cleanup_claimed_at" not in present:
            batch.add_column(sa.Column("cleanup_claimed_at", sa.DateTime(timezone=True), nullable=True))
        if "volume_exhausted_at" not in present:
            batch.add_column(sa.Column("volume_exhausted_at", sa.DateTime(timezone=True), nullable=True))
    names = _indexes(bind)
    if EXPIRE_INDEX not in names:
        op.create_index(EXPIRE_INDEX, TABLE, ["quota_expire_at"])
    if VOLUME_INDEX not in names:
        op.create_index(
            VOLUME_INDEX,
            TABLE,
            ["volume_exhausted_at"],
            sqlite_where=sa.text("volume_exhausted_at IS NOT NULL"),
            postgresql_where=sa.text("volume_exhausted_at IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    names = _indexes(bind)
    for name in (VOLUME_INDEX, EXPIRE_INDEX):
        if name in names:
            op.drop_index(name, table_name=TABLE)
    present = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for column in COLUMNS:
            if column in present:
                batch.drop_column(column)
