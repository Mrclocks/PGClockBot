"""Service transfers: a customer hands a service to another customer of the same shop.

Revision ID: 0126_service_transfers
Revises: 0125_service_freezes

Adds ``service_transfers``: a one-time link (only the SHA-256 of its token is stored) with an
expiry. A partial unique index allows one open transfer per service; the token hash is unique.
Idempotent; the downgrade drops the table (a transfer already accepted keeps its new owner).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0126_service_transfers"
down_revision: Union[str, None] = "0125_service_freezes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "service_transfers"
OPEN = sa.text("status = 'open'")


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("user_services.id", ondelete="CASCADE"), nullable=False),
            sa.Column("shop_id", sa.Integer(), nullable=True),
            sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=False),
            sa.Column("to_user_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=True),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="open"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if "uq_service_transfers_token" not in existing:
        op.create_index("uq_service_transfers_token", TABLE, ["token_hash"], unique=True)
    if "ix_service_transfers_service_created" not in existing:
        op.create_index("ix_service_transfers_service_created", TABLE, ["service_id", "created_at"])
    if "uq_service_transfers_open" not in existing:
        op.create_index(
            "uq_service_transfers_open", TABLE, ["service_id"], unique=True, sqlite_where=OPEN, postgresql_where=OPEN
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
