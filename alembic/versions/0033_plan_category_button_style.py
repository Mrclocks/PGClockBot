"""Add button_style to plan_categories for Telegram shop menu colors.

Revision ID: 0033_plan_category_button_style
Revises: 0032_plan_category_audience_reseller
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_plan_category_button_style"
down_revision: Union[str, None] = "0032_plan_category_audience_reseller"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("plan_categories"):
        return
    cols = {c["name"] for c in insp.get_columns("plan_categories")}
    if "button_style" not in cols:
        op.add_column(
            "plan_categories",
            sa.Column("button_style", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("plan_categories"):
        return
    cols = {c["name"] for c in insp.get_columns("plan_categories")}
    if "button_style" in cols:
        op.drop_column("plan_categories", "button_style")
