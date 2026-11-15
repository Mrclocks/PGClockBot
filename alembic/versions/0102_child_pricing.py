"""Level-1 price lists for level-2 shops, and linked parent charges.

Revision ID: 0102_child_pricing
Revises: 0101_cashback

Creates ``child_price_plans``; adds ``reseller_profiles.child_plan_id`` and
``reseller_charges.linked_charge_id`` (both nullable). Idempotent; the downgrade drops
only what this revision added.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0102_child_pricing"
down_revision: Union[str, None] = "0101_cashback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLANS = "child_price_plans"
ADDED = (("reseller_profiles", "child_plan_id"), ("reseller_charges", "linked_charge_id"))


def _columns(bind: sa.Connection, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(PLANS):
        references = [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("bot_users.id")]
        op.create_table(
            PLANS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("owner_shop_id", sa.Integer(), *references, nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("payg_basis", sa.String(16), nullable=False, server_default=sa.text("'traffic'")),
            sa.Column("payg_charge_upfront", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("price_per_gb", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("payg_user_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("payg_user_price_per_30d", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("payg_unlimited_price", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_child_price_plans_owner_shop_id", PLANS, ["owner_shop_id"])
    for table, column in ADDED:
        if inspector.has_table(table) and column not in _columns(bind, table):
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(column, sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, column in ADDED:
        if inspector.has_table(table) and column in _columns(bind, table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
    if inspector.has_table(PLANS):
        op.drop_table(PLANS)
