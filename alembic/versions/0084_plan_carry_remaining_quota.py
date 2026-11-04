"""Add Plan.carry_remaining_quota for owner-controlled renewal leftover transfer.

Revision ID: 0084_plan_carry_remaining_quota
Revises: 0083_shop_customer_marks

Existing plans keep carry on (server_default true) so renewals keep adding leftover
volume and time until an owner turns the switch off. Idempotent on SQLite and
PostgreSQL; downgrade drops the column and keeps every plan row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0084_plan_carry_remaining_quota"
down_revision: Union[str, None] = "0083_shop_customer_marks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "plans"
COLUMN = "carry_remaining_quota"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or COLUMN in _columns(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(
            sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or COLUMN not in _columns(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
