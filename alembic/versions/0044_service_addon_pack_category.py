"""Add category_id on service_addon_packs for per-category pricing.

Revision ID: 0044_service_addon_pack_category
Revises: 0043_reseller_plan_is_system
Create Date: 2026-10-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_service_addon_pack_category"
down_revision: Union[str, None] = "0043_reseller_plan_is_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("service_addon_packs"):
        return
    cols = {c["name"] for c in insp.get_columns("service_addon_packs")}
    if "category_id" in cols:
        return
    op.add_column(
        "service_addon_packs",
        sa.Column("category_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_service_addon_packs_category_id",
        "service_addon_packs",
        ["category_id"],
    )
    if insp.has_table("plan_categories"):
        op.create_foreign_key(
            "fk_service_addon_packs_category_id",
            "service_addon_packs",
            "plan_categories",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("service_addon_packs"):
        return
    cols = {c["name"] for c in insp.get_columns("service_addon_packs")}
    if "category_id" not in cols:
        return
    fks = {fk["name"] for fk in insp.get_foreign_keys("service_addon_packs")}
    if "fk_service_addon_packs_category_id" in fks:
        op.drop_constraint(
            "fk_service_addon_packs_category_id",
            "service_addon_packs",
            type_="foreignkey",
        )
    idxs = {i["name"] for i in insp.get_indexes("service_addon_packs")}
    if "ix_service_addon_packs_category_id" in idxs:
        op.drop_index(
            "ix_service_addon_packs_category_id",
            table_name="service_addon_packs",
        )
    op.drop_column("service_addon_packs", "category_id")
