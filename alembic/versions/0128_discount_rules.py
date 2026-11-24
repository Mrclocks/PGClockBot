"""Automatic discount rules and once-per-customer claims on the order discount ledger.

Revision ID: 0128_discount_rules
Revises: 0127_order_discounts

Adds ``discount_rules`` (a shop's automatic checkout discounts) and ``order_discounts.claim_key``
with a partial unique index over live lines, so a first-purchase discount or one per-customer
slot of a rule is held by one order at a time. Idempotent; the downgrade drops the table, the
index and the column and changes no order.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0128_discount_rules"
down_revision: Union[str, None] = "0127_order_discounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RULES = "discount_rules"
LEDGER = "order_discounts"
CLAIM_INDEX = "uq_order_discounts_live_claim"
LIVE_CLAIM = sa.text("claim_key IS NOT NULL AND released_at IS NULL")


def _create_rules() -> None:
    op.create_table(
        RULES,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("audience", sa.String(16), nullable=False, server_default=sa.text("'all'")),
        sa.Column("min_purchases", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("audience_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("events", sa.String(64), nullable=False, server_default=sa.text("'new'")),
        sa.Column("plan_ids", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False, server_default=sa.text("'percent'")),
        sa.Column("percent_bps", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fixed_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_per_order", sa.Integer(), nullable=True),
        sa.Column("min_order_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("customer_max_count", sa.Integer(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(RULES):
        _create_rules()
    if "ix_discount_rules_shop_id" not in {i["name"] for i in sa.inspect(bind).get_indexes(RULES)}:
        op.create_index("ix_discount_rules_shop_id", RULES, ["shop_id"])
    if "claim_key" not in {c["name"] for c in inspector.get_columns(LEDGER)}:
        op.add_column(LEDGER, sa.Column("claim_key", sa.String(32), nullable=True))
    if CLAIM_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(LEDGER)}:
        op.create_index(
            CLAIM_INDEX, LEDGER, ["shop_key", "user_id", "claim_key"], unique=True,
            postgresql_where=LIVE_CLAIM,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(LEDGER):
        if CLAIM_INDEX in {i["name"] for i in inspector.get_indexes(LEDGER)}:
            op.drop_index(CLAIM_INDEX, table_name=LEDGER)
        if "claim_key" in {c["name"] for c in sa.inspect(bind).get_columns(LEDGER)}:
            op.drop_column(LEDGER, "claim_key")
    if sa.inspect(bind).has_table(RULES):
        op.drop_table(RULES)
