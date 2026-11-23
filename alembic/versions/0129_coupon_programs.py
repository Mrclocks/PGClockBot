"""Automatic personal coupons: per-shop programs and a once-per-event key on entitlements.

Revision ID: 0129_coupon_programs
Revises: 0128_discount_rules

Adds ``coupon_programs`` (one row per shop and trigger) and
``loyalty_discount_entitlements.issue_key`` with a partial unique index, so a coupon for the
same event is never issued twice. Existing entitlements keep a NULL key. Idempotent; the
downgrade drops the table, the index and the column and keeps every entitlement.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0129_coupon_programs"
down_revision: Union[str, None] = "0128_discount_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROGRAMS = "coupon_programs"
ENTITLEMENTS = "loyalty_discount_entitlements"
ISSUE_INDEX = "uq_loyalty_discount_issue_key"
HAS_KEY = sa.text("issue_key IS NOT NULL")


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(PROGRAMS):
        op.create_table(
            PROGRAMS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_key", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("trigger", sa.String(24), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("percent", sa.Integer(), nullable=False, server_default=sa.text("10")),
            sa.Column("max_discount", sa.Integer(), nullable=True),
            sa.Column("min_purchase", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("valid_days", sa.Integer(), nullable=False, server_default=sa.text("14")),
            sa.Column("monthly_cap", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("shop_key", "trigger", name="uq_coupon_programs_shop_trigger"),
        )
    if not sa.inspect(bind).has_table(ENTITLEMENTS):
        return
    if "issue_key" not in {c["name"] for c in sa.inspect(bind).get_columns(ENTITLEMENTS)}:
        op.add_column(ENTITLEMENTS, sa.Column("issue_key", sa.String(64), nullable=True))
    if ISSUE_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(ENTITLEMENTS)}:
        op.create_index(ISSUE_INDEX, ENTITLEMENTS, ["issue_key"], unique=True,
                        postgresql_where=HAS_KEY)


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(ENTITLEMENTS):
        if ISSUE_INDEX in {i["name"] for i in sa.inspect(bind).get_indexes(ENTITLEMENTS)}:
            op.drop_index(ISSUE_INDEX, table_name=ENTITLEMENTS)
        if "issue_key" in {c["name"] for c in sa.inspect(bind).get_columns(ENTITLEMENTS)}:
            op.drop_column(ENTITLEMENTS, "issue_key")
    if sa.inspect(bind).has_table(PROGRAMS):
        op.drop_table(PROGRAMS)
