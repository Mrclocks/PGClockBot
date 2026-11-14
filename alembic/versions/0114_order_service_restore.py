"""Save the quota a renewal or add-on replaced.

Revision ID: 0114_order_service_restore
Revises: 0113_service_import_offer_dismissal

Adds the nullable ``orders.service_restore``: the service's panel quota (status, volume limit,
expiry, hold time) and plan from just before a renewal or add-on wrote to the panel, plus what
it wrote. A void of that order puts the quota back. Existing orders keep NULL and cannot be
voided this way. Idempotent; the downgrade drops the column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0114_order_service_restore"
down_revision: Union[str, None] = "0113_service_import_offer_dismissal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "orders"
COLUMN = "service_restore"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or COLUMN in _columns(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or COLUMN not in _columns(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
