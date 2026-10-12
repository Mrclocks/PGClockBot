"""Add icon_custom_emoji_id on plan_categories for premium Telegram icons.

Revision ID: 0045_plan_category_icon_emoji
Revises: 0044_service_addon_pack_category
Create Date: 2026-10-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_plan_category_icon_emoji"
down_revision: Union[str, None] = "0044_service_addon_pack_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("plan_categories"):
        return
    cols = {c["name"] for c in insp.get_columns("plan_categories")}
    if "icon_custom_emoji_id" in cols:
        return
    op.add_column(
        "plan_categories",
        sa.Column("icon_custom_emoji_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("plan_categories"):
        return
    cols = {c["name"] for c in insp.get_columns("plan_categories")}
    if "icon_custom_emoji_id" not in cols:
        return
    op.drop_column("plan_categories", "icon_custom_emoji_id")
