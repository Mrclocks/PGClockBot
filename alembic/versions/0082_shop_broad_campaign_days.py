"""How many campaigns to its whole audience a shop created per UTC day (daily cap).

Revision ID: 0082_shop_broad_campaign_days
Revises: 0081_shop_customer_block

One row per shop per UTC day (``day`` is ``YYYY-MM-DD``). The cap is claimed with a
conditional UPDATE on this row, so concurrent requests cannot both pass it.

Idempotent; the downgrade drops the table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0082_shop_broad_campaign_days"
down_revision: Union[str, None] = "0081_shop_customer_block"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "shop_broad_campaign_days"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("used", sa.BigInteger(), server_default="0", nullable=False),
        sa.UniqueConstraint("shop_id", "day", name="uq_shop_broad_campaign_days_shop_day"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
