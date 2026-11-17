"""How many targeted group messages and campaigns a shop sent per UTC day (daily cap).

Revision ID: 0118_shop_targeted_message_days
Revises: 0117_broadcast_audience_spec

Messages to a narrower segment than «همه» or «سرویس فعال» do not use the shop's one broad slot of the day; this
table caps them separately. One row per shop per UTC day (``day`` is ``YYYY-MM-DD``), claimed with a conditional
UPDATE like ``shop_broad_campaign_days``, so concurrent requests cannot both pass.

Idempotent; the downgrade drops the table (it only holds counters).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0118_shop_targeted_message_days"
down_revision: Union[str, None] = "0117_broadcast_audience_spec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "shop_targeted_message_days"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("used", sa.BigInteger(), server_default="0", nullable=False),
        sa.UniqueConstraint("shop_id", "day", name="uq_shop_targeted_message_days_shop_day"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
