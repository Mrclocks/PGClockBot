"""Service freezes: a customer pauses a service and keeps the days that were left.

Revision ID: 0125_service_freezes
Revises: 0124_cashback_topup_grants

Adds ``service_freezes``: one row per pause, with the time left saved before the panel call and
the target expiry saved before the resume call. A partial unique index allows one open freeze per
service. Idempotent; the downgrade drops the table (past freezes are history only; a service that
is still frozen stays disabled on the panel and is resumed by staff).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0125_service_freezes"
down_revision: Union[str, None] = "0124_cashback_topup_grants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "service_freezes"
OPEN = sa.text("status IN ('pausing', 'frozen', 'resuming')")


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("user_services.id", ondelete="CASCADE"), nullable=False),
            sa.Column("shop_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pausing"),
            sa.Column("remaining_seconds", sa.Integer(), nullable=False),
            sa.Column("resume_by", sa.DateTime(timezone=True), nullable=False),
            sa.Column("target_expire", sa.BigInteger(), nullable=True),
            sa.Column("resumed_by", sa.String(16), nullable=True),
            sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if "ix_service_freezes_status_resume_by" not in existing:
        op.create_index("ix_service_freezes_status_resume_by", TABLE, ["status", "resume_by"])
    if "ix_service_freezes_service_created" not in existing:
        op.create_index("ix_service_freezes_service_created", TABLE, ["service_id", "created_at"])
    if "uq_service_freezes_open" not in existing:
        op.create_index(
            "uq_service_freezes_open", TABLE, ["service_id"], unique=True, postgresql_where=OPEN
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
