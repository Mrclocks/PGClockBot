"""Remember why the expired-service cleanup could not handle a service.

Revision ID: 0145_service_cleanup_errors
Revises: 0144_payment_receipts

Adds ``cleanup_error`` (a short code such as ``panel_unreadable``) and ``cleanup_error_at`` to
``user_services``, with a partial index on ``shop_id`` for the rows that have one, so the cleanup
page can summarise the failures of one shop. Idempotent; the downgrade drops only the new columns
and the index and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0145_service_cleanup_errors"
down_revision: Union[str, None] = "0144_payment_receipts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_services"
ERROR_INDEX = "ix_user_services_cleanup_error"
COLUMNS = (
    ("cleanup_error", sa.String(32)),
    ("cleanup_error_at", sa.DateTime(timezone=True)),
)


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = _columns(bind)
    for name, column_type in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, column_type, nullable=True))
    if ERROR_INDEX not in _indexes(bind):
        op.create_index(ERROR_INDEX, TABLE, ["shop_id"], postgresql_where=sa.text("cleanup_error IS NOT NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if ERROR_INDEX in _indexes(bind):
        op.drop_index(ERROR_INDEX, table_name=TABLE)
    present = _columns(bind)
    for name, _type in COLUMNS:
        if name in present:
            op.drop_column(TABLE, name)
