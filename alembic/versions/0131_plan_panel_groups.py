"""Sale plans whose PasarGuard groups the panel assigns.

Revision ID: 0131_plan_panel_groups
Revises: 0130_discount_budget_winback

Adds ``plans.pg_panel_groups`` (NOT NULL, default false). A group-mode plan with
this flag stores no groups and leaves the choice to the shop's PasarGuard admin.
Idempotent; the downgrade drops the column and keeps every plan.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0131_plan_panel_groups"
down_revision: Union[str, None] = "0130_discount_budget_winback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "plans"
COLUMN = "pg_panel_groups"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(TABLE) and COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE) and not _has_column():
        op.add_column(TABLE, sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
