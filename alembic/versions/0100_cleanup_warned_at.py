"""When the last cleanup warning was queued.

Revision ID: 0100_cleanup_warned_at
Revises: 0099_cleanup_volume_warn_stage

Adds ``user_services.cleanup_warned_at`` and dates the services that already are in a warning stage with
the moment of the upgrade. Idempotent; the downgrade drops only that column and keeps
every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0100_cleanup_warned_at"
down_revision: Union[str, None] = "0099_cleanup_volume_warn_stage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_services"
COLUMN = "cleanup_warned_at"


def _has_column(bind: sa.Connection) -> bool:
    return COLUMN in {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or _has_column(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
    # Services already in a warning stage had their warning before this column existed: date it now, so the
    # deletion (which needs a warning date at least a few hours old) waits for them like any other.
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    stages = [c for c in ("cleanup_warn_stage", "cleanup_volume_warn_stage") if c in columns]
    if stages:
        condition = " OR ".join(f"{c} >= 1" for c in stages)
        op.execute(sa.text(f"UPDATE {TABLE} SET {COLUMN} = CURRENT_TIMESTAMP WHERE {condition}"))  # noqa: S608 - fixed identifiers


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or not _has_column(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
