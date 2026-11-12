"""Remember who dismissed the home offer to add a previous service.

Revision ID: 0112_service_import_offer_dismissal
Revises: 0111_referral_shop

One row per customer and shop (``shop_key`` 0 is the platform) once they pick «نمایش نده».
Idempotent (the table is only created when missing); the downgrade drops it, which only means
the offer can appear again.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0112_service_import_offer_dismissal"
down_revision: Union[str, None] = "0111_referral_shop"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "service_import_offer_dismissals"


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("shop_key", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
