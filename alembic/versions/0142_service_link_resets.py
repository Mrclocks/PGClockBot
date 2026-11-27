"""Service link resets: a customer changes the subscription link of their own service.

Revision ID: 0142_service_link_resets
Revises: 0141_ticket_reminders

Adds ``service_link_resets``: who changed which service's link and when, its status, and the SHA-256
of the token it replaced (to tell later whether the panel changed it when its answer was lost). A
partial unique index allows one open change per service. Idempotent; the downgrade drops the table
(services keep the link they have).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0142_service_link_resets"
down_revision: Union[str, None] = "0141_ticket_reminders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "service_link_resets"
OPEN = sa.text("status IN ('pending', 'unknown')")


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "service_id", sa.Integer(), sa.ForeignKey("user_services.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("shop_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=False),
            sa.Column("pg_user_id", sa.Integer(), nullable=False),
            sa.Column("old_token_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if "ix_service_link_resets_service_created" not in existing:
        op.create_index("ix_service_link_resets_service_created", TABLE, ["service_id", "created_at"])
    if "ix_service_link_resets_open_id" not in existing:
        op.create_index("ix_service_link_resets_open_id", TABLE, ["id"], postgresql_where=OPEN)
    if "uq_service_link_resets_open" not in existing:
        op.create_index("uq_service_link_resets_open", TABLE, ["service_id"], unique=True, postgresql_where=OPEN)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
