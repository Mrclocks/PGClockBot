"""Discount codes limited to some of the shop's plans.

Revision ID: 0110_charge_code_plan_ids
Revises: 0109_order_staff_notified_at

Adds the nullable ``charge_codes.plan_ids`` (comma-separated plan ids). Existing codes
keep NULL, which still means every plan. Idempotent; the downgrade drops only that
column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0110_charge_code_plan_ids"
down_revision: Union[str, None] = "0109_order_staff_notified_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "charge_codes"
COLUMN = "plan_ids"


def _has_column(bind: sa.Connection) -> bool:
    return COLUMN in {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or _has_column(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.String(512), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE) or not _has_column(bind):
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
