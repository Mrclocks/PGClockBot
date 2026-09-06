"""Terms acceptance ledger for entry/purchase rules gates.

Revision ID: 0025_terms_acceptances
Revises: 0024_user_service_quota_cache
Create Date: 2026-09-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_terms_acceptances"
down_revision: Union[str, None] = "0024_user_service_quota_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("terms_acceptances"):
        return
    op.create_table(
        "terms_acceptances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bot_user_id", sa.Integer(), nullable=False),
        sa.Column("shop_owner_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gate", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bot_user_id"], ["bot_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_user_id",
            "shop_owner_id",
            "gate",
            name="uq_terms_acceptances_user_shop_gate",
        ),
    )
    op.create_index("ix_terms_acceptances_bot_user_id", "terms_acceptances", ["bot_user_id"])
    op.create_index("ix_terms_acceptances_shop_owner_id", "terms_acceptances", ["shop_owner_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("terms_acceptances"):
        return
    op.drop_index("ix_terms_acceptances_shop_owner_id", table_name="terms_acceptances")
    op.drop_index("ix_terms_acceptances_bot_user_id", table_name="terms_acceptances")
    op.drop_table("terms_acceptances")
