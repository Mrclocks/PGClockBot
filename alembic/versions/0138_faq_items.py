"""Frequently asked questions shown as buttons under «پشتیبانی».

Revision ID: 0138_faq_items
Revises: 0137_broadcast_media

Adds ``faq_items``: one row per question, scoped to a shop by ``reseller_id`` (NULL for the
platform bot), ordered by ``sort_order``. Idempotent: the table and its index are only created
when missing. The downgrade drops only this new table; the old single ``faq_text`` setting is
never touched, so it is still there after a downgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0138_faq_items"
down_revision: Union[str, None] = "0137_broadcast_media"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "faq_items"
ORDER_INDEX = "ix_faq_items_shop_order"


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "reseller_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("question", sa.String(160), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("button_style", sa.String(16), nullable=True),
        sa.Column("icon_custom_emoji_id", sa.String(32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ticket_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        _create_table()
        inspector = sa.inspect(op.get_bind())
    existing = {index["name"] for index in inspector.get_indexes(TABLE)}
    if ORDER_INDEX not in existing:
        op.create_index(ORDER_INDEX, TABLE, ["reseller_id", "sort_order", "id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
