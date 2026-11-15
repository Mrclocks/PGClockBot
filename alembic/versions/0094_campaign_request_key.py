"""Idempotent request key for targeted campaigns queued from the bot.

Revision ID: 0094_campaign_request_key
Revises: 0093_wallet_tx_idempotency

Adds a nullable ``request_key`` on ``targeted_campaigns``. A confirmation tap that is
repeated or raced carries the same key, so at most one campaign is created for it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0094_campaign_request_key"
down_revision: Union[str, None] = "0093_wallet_tx_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "targeted_campaigns"
COLUMN = "request_key"
INDEX = "uq_targeted_campaigns_request_key"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    if COLUMN not in {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(64), nullable=True))
    if INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(
            INDEX,
            TABLE,
            [COLUMN],
            unique=True,
            sqlite_where=sa.text(f"{COLUMN} IS NOT NULL"),
            postgresql_where=sa.text(f"{COLUMN} IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}:
        op.drop_column(TABLE, COLUMN)
