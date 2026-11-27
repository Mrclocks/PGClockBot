"""Owner turns a customer or a shop off together with its panel services.

Revision ID: 0136_account_holds
Revises: 0135_broadcast_schedule

Adds ``account_holds`` (one open hold per target and scope, partial unique index) and
``account_hold_items`` (the panel users a hold turned off). Idempotent; the downgrade drops
only these two new tables and leaves every existing row alone.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0136_account_holds"
down_revision: Union[str, None] = "0135_broadcast_schedule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HOLDS = "account_holds"
ITEMS = "account_hold_items"
OPEN = sa.text("status <> 'released'")


def _create_holds() -> None:
    op.create_table(
        HOLDS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_user_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope_shop_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="applying"),
        sa.Column("parent_hold_id", sa.Integer(), sa.ForeignKey("account_holds.id", ondelete="CASCADE"), nullable=True),
        sa.Column("pg_admin_username", sa.String(128), nullable=True),
        sa.Column("admin_state", sa.String(16), nullable=False, server_default="none"),
        sa.Column("restore_shop_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("items_listed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("restore_unblock", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("placed_by_shop_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )


def _create_items() -> None:
    op.create_table(
        ITEMS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("hold_id", sa.Integer(), sa.ForeignKey("account_holds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("user_services.id", ondelete="SET NULL"), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("hold_id", "pg_user_id", name="uq_account_hold_items_user"),
    )


INDEXES: tuple[tuple[str, str, tuple[str, ...], dict], ...] = (
    ("uq_account_holds_open", HOLDS, ("kind", "target_user_id", "scope_shop_id"),
     {"unique": True, "postgresql_where": OPEN}),
    ("ix_account_holds_status_id", HOLDS, ("status", "id"), {}),
    ("ix_account_holds_parent", HOLDS, ("parent_hold_id",), {}),
    ("ix_account_holds_admin_open", HOLDS, ("pg_admin_username",), {"postgresql_where": OPEN}),
    ("ix_account_hold_items_pg_user", ITEMS, ("pg_user_id",), {}),
    ("ix_account_hold_items_hold_state", ITEMS, ("hold_id", "state"), {}),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(HOLDS):
        _create_holds()
    if not inspector.has_table(ITEMS):
        _create_items()
    inspector = sa.inspect(op.get_bind())
    existing = {ix["name"] for table in (HOLDS, ITEMS) for ix in inspector.get_indexes(table)}
    for name, table, columns, options in INDEXES:
        if name not in existing:
            op.create_index(name, table, list(columns), **options)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (ITEMS, HOLDS):
        if inspector.has_table(table):
            op.drop_table(table)
