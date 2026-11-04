"""ResellerPlan switches for leftover period and purchased extras on renew.

Revision ID: 0085_reseller_plan_carry_on_renew
Revises: 0084_plan_carry_remaining_quota

Existing packages keep both switches on so renewals still extend from a future
expiry and keep bought extras until an owner turns them off. Idempotent on
SQLite and PostgreSQL; downgrade drops the columns and keeps every plan row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0085_reseller_plan_carry_on_renew"
down_revision: Union[str, None] = "0084_plan_carry_remaining_quota"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "reseller_plans"
COLUMNS = ("carry_remaining_period", "keep_extras_on_renew")


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    existing = _columns(bind)
    missing = [name for name in COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in missing:
            batch.add_column(
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true())
            )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    existing = _columns(bind)
    drop = [name for name in COLUMNS if name in existing]
    if not drop:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in drop:
            batch.drop_column(name)
