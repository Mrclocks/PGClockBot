"""Remember which services the panel no longer knows.

Revision ID: 0104_service_panel_presence
Revises: 0103_payg_baseline_at

Adds ``panel_missing_at`` (a confirmed HTTP 404 on the panel user lookup) and
``panel_checked_at`` (when the presence scan last looked) to ``user_services``, with an
index for the scan order and a partial index for the missing rows. Idempotent; the
downgrade drops only the new columns and indexes and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0104_service_panel_presence"
down_revision: Union[str, None] = "0103_payg_baseline_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_services"
CHECKED_INDEX = "ix_user_services_panel_checked_at"
MISSING_INDEX = "ix_user_services_panel_missing"


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
        if "panel_missing_at" not in present:
            batch.add_column(sa.Column("panel_missing_at", sa.DateTime(timezone=True), nullable=True))
        if "panel_checked_at" not in present:
            batch.add_column(sa.Column("panel_checked_at", sa.DateTime(timezone=True), nullable=True))
    names = _indexes(bind)
    if CHECKED_INDEX not in names:
        op.create_index(CHECKED_INDEX, TABLE, ["panel_checked_at"])
    if MISSING_INDEX not in names:
        op.create_index(
            MISSING_INDEX,
            TABLE,
            ["shop_id"],
            sqlite_where=sa.text("panel_missing_at IS NOT NULL"),
            postgresql_where=sa.text("panel_missing_at IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    names = _indexes(bind)
    for name in (MISSING_INDEX, CHECKED_INDEX):
        if name in names:
            op.drop_index(name, table_name=TABLE)
    present = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for column in ("panel_missing_at", "panel_checked_at"):
            if column in present:
                batch.drop_column(column)
