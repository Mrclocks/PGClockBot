"""Which API gateway issued a PSP checkout.

Revision ID: 0071_settlement_psp_gateway
Revises: 0070_shop_miniapp_key

A shop can configure several API gateways; the verify of a checkout must use the
credential of the gateway that created it. Existing rows keep NULL, meaning the single
gateway of the older settings. Idempotent; the downgrade drops the column and keeps rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0071_settlement_psp_gateway"
down_revision: Union[str, None] = "0070_shop_miniapp_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "payment_settlements"
COLUMN = "gateway_id"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if COLUMN not in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.String(32), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if COLUMN in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
