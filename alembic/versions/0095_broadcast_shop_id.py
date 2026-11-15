"""Shop-scoped broadcast logs.

Revision ID: 0095_broadcast_shop_id
Revises: 0093_wallet_tx_idempotency

Adds nullable ``shop_id`` (platform rows stay NULL) and optional ``request_key`` so a
shop broadcast is isolated from the owner queue and a repeated submit cannot enqueue
twice. Idempotent; the downgrade drops only the new columns and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0095_broadcast_shop_id"
down_revision: Union[str, None] = "0094_campaign_request_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "broadcast_logs"
SHOP_INDEX = "ix_broadcast_logs_shop_id"
REQUEST_INDEX = "uq_broadcast_logs_request_key"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = _columns(bind)
    references = [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("bot_users.id")]
    with op.batch_alter_table(TABLE) as batch:
        if "shop_id" not in present:
            batch.add_column(sa.Column("shop_id", sa.Integer(), *references, nullable=True))
        if "request_key" not in present:
            batch.add_column(sa.Column("request_key", sa.String(64), nullable=True))
    names = _indexes(bind)
    if SHOP_INDEX not in names:
        op.create_index(SHOP_INDEX, TABLE, ["shop_id"])
    if REQUEST_INDEX not in names:
        op.create_index(
            REQUEST_INDEX,
            TABLE,
            ["request_key"],
            unique=True,
            sqlite_where=sa.text("request_key IS NOT NULL"),
            postgresql_where=sa.text("request_key IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    names = _indexes(bind)
    if REQUEST_INDEX in names:
        op.drop_index(REQUEST_INDEX, table_name=TABLE)
    if SHOP_INDEX in names:
        op.drop_index(SHOP_INDEX, table_name=TABLE)
    present = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        if "request_key" in present:
            batch.drop_column("request_key")
        if "shop_id" in present:
            batch.drop_column("shop_id")
