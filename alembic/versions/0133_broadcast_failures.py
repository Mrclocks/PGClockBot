"""Why group-message recipients were not reached, and who a tenant's bot cannot message.

Revision ID: 0133_broadcast_failures
Revises: 0132_cancellation_refund_quote

Adds ``broadcast_failures`` (one row per recipient a broadcast did not reach, with the kind of
reason) and ``broadcast_unreachable`` (people who blocked a tenant's bot, deleted their account or
never started it; group messages skip them). Earlier broadcasts keep their counts without reasons.
Idempotent; the downgrade drops both tables and keeps every broadcast.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0133_broadcast_failures"
down_revision: Union[str, None] = "0132_cancellation_refund_quote"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FAILURES = "broadcast_failures"
UNREACHABLE = "broadcast_unreachable"
FAILURE_USER_INDEX = "ix_broadcast_failures_bot_user_id"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(FAILURES):
        op.create_table(
            FAILURES,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "broadcast_id", sa.Integer(), sa.ForeignKey("broadcast_logs.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("bot_user_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("broadcast_id", "bot_user_id", name="uq_broadcast_failures_recipient"),
        )
    if FAILURE_USER_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(FAILURES)}:
        op.create_index(FAILURE_USER_INDEX, FAILURES, ["bot_user_id"])
    if not sa.inspect(bind).has_table(UNREACHABLE):
        op.create_table(
            UNREACHABLE,
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("shop_key", sa.Integer(), primary_key=True),
            sa.Column("reason", sa.String(24), nullable=False),
            sa.Column("marked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(UNREACHABLE):
        op.drop_table(UNREACHABLE)
    if sa.inspect(bind).has_table(FAILURES):
        op.drop_table(FAILURES)
