"""Admin approval of new customers before the bot opens its menus.

Revision ID: 0114_entry_approvals
Revises: 0113_service_import_offer_dismissal

One row per customer and shop (``shop_key`` 0 is the platform). Idempotent (the table and its
index are only created when missing); the downgrade drops the table, which only means pending
requests are forgotten and every customer can use the bot again.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0114_entry_approvals"
down_revision: Union[str, None] = "0113_service_import_offer_dismissal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "entry_approvals"
STATUS_INDEX = "ix_entry_approvals_shop_status"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("bot_user_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("shop_key", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("status", sa.String(16), server_default="pending", nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_by", sa.String(64), nullable=True),
            sa.Column("admin_notified_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("bot_user_id", "shop_key", name="uq_entry_approvals_user_shop"),
        )
    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}
    if STATUS_INDEX not in indexes:
        op.create_index(STATUS_INDEX, TABLE, ["shop_key", "status", "requested_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
