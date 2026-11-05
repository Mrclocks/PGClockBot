"""Orders keep the service name the buyer chose at checkout.

Revision ID: 0085_order_requested_username
Revises: 0084_reseller_plan_extra_packs

``orders`` gains a nullable ``requested_username``. Delivery puts it in place of the
naming pattern's ``{random}`` part. Existing orders keep NULL (generated names).
Idempotent; the downgrade drops the column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0085_order_requested_username"
down_revision: Union[str, None] = "0084_reseller_plan_extra_packs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "orders"
COLUMN = "requested_username"


def _has_column(bind: sa.Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return False
    return COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or _has_column(bind):
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(32), nullable=True))


def downgrade() -> None:
    if not _has_column(op.get_bind()):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
