"""Queue of customer notices about staff actions.

Revision ID: 0105_customer_notice_queue
Revises: 0104_service_panel_presence

Creates ``customer_notices``: one row per message, claimed by a throttled worker before it is sent.
Idempotent; the downgrade drops the table (queued notices are transient).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0105_customer_notice_queue"
down_revision: Union[str, None] = "0104_service_panel_presence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "customer_notices"
INDEX = "ix_customer_notices_status_due"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_id", sa.Integer(), nullable=True),
            sa.Column("bot_user_id", sa.Integer(), nullable=False),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("service_id", sa.Integer(), nullable=True),
            sa.Column("action", sa.String(32), nullable=False),
            sa.Column("service_name", sa.String(128), nullable=False, server_default=""),
            sa.Column("reason", sa.String(255), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    names = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if INDEX not in names:
        op.create_index(INDEX, TABLE, ["status", "next_attempt_at", "id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    if INDEX in {index["name"] for index in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
