"""Panel snapshots for PAYG reconciliation of changes made outside the bot.

Revision ID: 0098_payg_panel_snapshots
Revises: 0097_payg_charges

Creates ``payg_panel_snapshots`` (one row per shop and panel user). Idempotent; the
downgrade drops only this table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0098_payg_panel_snapshots"
down_revision: Union[str, None] = "0097_payg_charges"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "payg_panel_snapshots"
SHOP_INDEX = "ix_payg_panel_snapshots_shop_id"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        references = [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("bot_users.id")]
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_id", sa.Integer(), *references, nullable=False),
            sa.Column("pg_user_id", sa.Integer(), nullable=False),
            sa.Column("pg_username", sa.String(128), nullable=False),
            sa.Column("data_limit", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("expire_ts", sa.BigInteger(), nullable=True),
            sa.Column("used_traffic", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("shop_id", "pg_user_id", name="uq_payg_snapshot_user"),
        )
    if SHOP_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(SHOP_INDEX, TABLE, ["shop_id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
