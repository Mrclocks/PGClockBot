"""When staff were told about a sale made outside the bot.

Revision ID: 0109_order_staff_notified_at
Revises: 0108_cleanup_warned_at

Adds the nullable ``orders.staff_notified_at``. A Mini App purchase claims it before
the staff notice is sent, so a retried or repeated request does not send it twice.
Idempotent; the downgrade drops only that column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0109_order_staff_notified_at"
down_revision: Union[str, None] = "0108_cleanup_warned_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "orders"
COLUMN = "staff_notified_at"


def _has_column(bind: sa.Connection) -> bool:
    return COLUMN in {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or _has_column(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or not _has_column(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
