"""Monthly budget for automatic discount rules and the win-back coupon's inactivity days.

Revision ID: 0130_discount_budget_winback
Revises: 0129_coupon_programs

Adds ``discount_budgets`` (one row per shop and month) and ``coupon_programs.inactive_days``
(NOT NULL, default 0). Idempotent; the downgrade drops both and keeps every program.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0130_discount_budget_winback"
down_revision: Union[str, None] = "0129_coupon_programs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUDGETS = "discount_budgets"
PROGRAMS = "coupon_programs"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(BUDGETS):
        op.create_table(
            BUDGETS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_key", sa.Integer(), nullable=False),
            sa.Column("month", sa.String(7), nullable=False),
            sa.Column("used", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.UniqueConstraint("shop_key", "month", name="uq_discount_budget_month"),
        )
    if sa.inspect(bind).has_table(PROGRAMS) and "inactive_days" not in {
        c["name"] for c in sa.inspect(bind).get_columns(PROGRAMS)
    }:
        with op.batch_alter_table(PROGRAMS) as batch:
            batch.add_column(sa.Column("inactive_days", sa.Integer(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(PROGRAMS) and "inactive_days" in {
        c["name"] for c in sa.inspect(bind).get_columns(PROGRAMS)
    }:
        with op.batch_alter_table(PROGRAMS) as batch:
            batch.drop_column("inactive_days")
    if sa.inspect(bind).has_table(BUDGETS):
        op.drop_table(BUDGETS)
