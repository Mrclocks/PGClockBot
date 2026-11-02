"""Add ResellerProfile.allow_volume_oversell and the volume_holds table for the volume gate.

Revision ID: 0065_allow_volume_oversell
Revises: 0064_settlement_notified_at

Existing shops keep oversell on (server_default true) so behaviour does not change
until an Owner turns the switch off. New shops get false in application code.
volume_holds keeps volume promised to an in-flight panel call; rows are short-lived.
Idempotent on SQLite and PostgreSQL; downgrade drops the column and the table, keeps rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065_allow_volume_oversell"
down_revision: Union[str, None] = "0064_settlement_notified_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "reseller_profiles"
COLUMN = "allow_volume_oversell"
HOLDS = "volume_holds"
HOLDS_INDEX = "ix_volume_holds_shop_expires"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _add_switch(bind: sa.Connection) -> None:
    if not sa.inspect(bind).has_table(TABLE) or COLUMN in _columns(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        # sa.true() renders 1 on SQLite and true on PostgreSQL (a bare 1 is rejected there).
        batch.add_column(sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.true()))


def _add_holds(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    if not inspector.has_table(HOLDS):
        op.create_table(
            HOLDS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_id", sa.Integer(), nullable=False),
            sa.Column("hold_key", sa.String(96), nullable=False, unique=True),
            sa.Column("bytes", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        inspector = sa.inspect(bind)
    if HOLDS_INDEX not in {index["name"] for index in inspector.get_indexes(HOLDS)}:
        op.create_index(HOLDS_INDEX, HOLDS, ["shop_id", "expires_at"])


def upgrade() -> None:
    bind = op.get_bind()
    _add_switch(bind)
    _add_holds(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(HOLDS):
        op.drop_table(HOLDS)
    if not sa.inspect(bind).has_table(TABLE) or COLUMN not in _columns(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
