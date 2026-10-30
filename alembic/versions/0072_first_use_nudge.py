"""First-use check of new services.

Revision ID: 0072_first_use_nudge
Revises: 0071_settlement_psp_gateway

``user_services.first_use_checked_at`` records that the first-use check ran for a service, so the
check and its message happen once. A partial index keeps the lookup of unchecked services cheap.
Existing rows keep NULL; the job only looks at services created in the last three days. Idempotent;
the downgrade drops the index and the column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0072_first_use_nudge"
down_revision: Union[str, None] = "0071_settlement_psp_gateway"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_services"
COLUMN = "first_use_checked_at"
INDEX = "ix_user_services_first_use_pending"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if COLUMN not in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
    if INDEX not in _indexes(bind):
        where = sa.text(f"{COLUMN} IS NULL")
        op.create_index(INDEX, TABLE, ["created_at"], sqlite_where=where, postgresql_where=where)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if INDEX in _indexes(bind):
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
