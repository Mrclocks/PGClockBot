"""Credit lines for PAYG shops and their audit log.

Revision ID: 0099_shop_credit
Revises: 0098_payg_panel_snapshots

Creates ``shop_credit_accounts`` (one per shop) and ``credit_events``. No existing row
changes: a shop without an account has no credit. Idempotent; the downgrade drops only
these tables.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0099_shop_credit"
down_revision: Union[str, None] = "0098_payg_panel_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACCOUNTS = "shop_credit_accounts"
EVENTS = "credit_events"
EVENTS_INDEX = "ix_credit_events_shop_id"


def _shop_column(bind: sa.Connection, **kwargs) -> sa.Column:
    references = [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("bot_users.id")]
    return sa.Column("shop_id", sa.Integer(), *references, nullable=False, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(ACCOUNTS):
        op.create_table(
            ACCOUNTS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _shop_column(bind, unique=True),
            sa.Column("credit_limit", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("temp_limit", sa.Integer(), nullable=True),
            sa.Column("temp_limit_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
            sa.Column("alert_level", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("restriction", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    if not inspector.has_table(EVENTS):
        op.create_table(
            EVENTS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            _shop_column(bind),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("old_value", sa.String(64), nullable=True),
            sa.Column("new_value", sa.String(64), nullable=True),
            sa.Column("actor", sa.String(128), nullable=True),
            sa.Column("note", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    if EVENTS_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(EVENTS)}:
        op.create_index(EVENTS_INDEX, EVENTS, ["shop_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (EVENTS, ACCOUNTS):
        if inspector.has_table(table):
            op.drop_table(table)
