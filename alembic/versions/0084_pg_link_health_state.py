"""Persist PasarGuard link-health summary across restarts.

Revision ID: 0084_pg_link_health_state
Revises: 0083_shop_customer_marks

Single platform row (id=1) holds last_ok / last_failure / pause_count and JSON
per-kind totals for the last 24h. Idempotent; downgrade drops the table and
keeps every other row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0084_pg_link_health_state"
down_revision: Union[str, None] = "0083_shop_customer_marks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "pg_link_health_state"


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False, nullable=False),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_kind", sa.String(length=32), nullable=True),
        sa.Column("pause_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind_totals_24h", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
