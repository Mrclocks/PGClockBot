"""Shop cashback: rules, one grant per delivered order, and monthly budgets.

Revision ID: 0101_cashback
Revises: 0100_credit_statements

New tables only. Idempotent; the downgrade drops them.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0101_cashback"
down_revision: Union[str, None] = "0100_credit_statements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fk(bind: sa.Connection) -> list:
    return [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("bot_users.id")]


def _rules(bind: sa.Connection) -> None:
    op.create_table(
        "cashback_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.Integer(), *_fk(bind), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mode", sa.String(16), nullable=False, server_default=sa.text("'percent'")),
        sa.Column("percent_bps", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fixed_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_per_order", sa.Integer(), nullable=True),
        sa.Column("min_order_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("events", sa.String(64), nullable=False, server_default=sa.text("'new,renew,addon'")),
        sa.Column("plan_ids", sa.Text(), nullable=True),
        sa.Column("payment_methods", sa.String(255), nullable=True),
        sa.Column("first_purchase_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("customer_max_count", sa.Integer(), nullable=True),
        sa.Column("customer_max_amount", sa.Integer(), nullable=True),
        sa.Column("allow_with_discount", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_cashback_rules_shop_id", "cashback_rules", ["shop_id"])


def _grants(bind: sa.Connection) -> None:
    op.create_table(
        "cashback_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), *_fk(bind), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("base_amount", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unrecoverable_amount", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("order_id", name="uq_cashback_grant_order"),
    )
    op.create_index("ix_cashback_grants_status_available", "cashback_grants", ["status", "available_at"])
    op.create_index("ix_cashback_grants_shop_user_created", "cashback_grants", ["shop_id", "user_id", "created_at"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("cashback_rules"):
        _rules(bind)
    if not inspector.has_table("cashback_grants"):
        _grants(bind)
    if not inspector.has_table("cashback_budgets"):
        op.create_table(
            "cashback_budgets",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_key", sa.Integer(), nullable=False),
            sa.Column("month", sa.String(7), nullable=False),
            sa.Column("used", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.UniqueConstraint("shop_key", "month", name="uq_cashback_budget_month"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("cashback_budgets", "cashback_grants", "cashback_rules"):
        if inspector.has_table(table):
            op.drop_table(table)
