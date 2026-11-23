"""Timed quick switches and scenarios: the values to put back.

Revision ID: 0122_switch_reverts
Revises: 0121_split_renewal_carry

Adds the ``switch_reverts`` table with an index for the due-revert lookup and a partial unique
index allowing one pending revert per shop and setting. Idempotent; the downgrade drops the table,
which only ever holds pending or finished reverts (the switch values themselves stay in settings).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0122_switch_reverts"
down_revision: Union[str, None] = "0121_split_renewal_carry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "switch_reverts"
PENDING = sa.text("status = 'pending'")


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("setting_key", sa.String(64), nullable=False),
            sa.Column("set_value", sa.Boolean(), nullable=False),
            sa.Column("revert_value", sa.Boolean(), nullable=False),
            sa.Column("revert_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scenario", sa.String(24), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if "ix_switch_reverts_status_revert_at" not in existing:
        op.create_index("ix_switch_reverts_status_revert_at", TABLE, ["status", "revert_at"])
    if "uq_switch_reverts_pending" not in existing:
        op.create_index(
            "uq_switch_reverts_pending",
            TABLE,
            ["shop_id", "setting_key"],
            unique=True,
            sqlite_where=PENDING,
            postgresql_where=PENDING,
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
