"""Add Plan customer extra unit rates and buy-extra switch.

Revision ID: 0090_plan_customer_extra_rates
Revises: 0089_addon_request_key

Mirrors reseller fixed-plan stairs for customer fixed plans: allow_buy_extra,
extra_gb_price (toman/GB), extra_month_price (toman/month). Idempotent on
SQLite and PostgreSQL; downgrade drops the three columns and keeps plan rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0090_plan_customer_extra_rates"
down_revision: Union[str, None] = "0089_addon_request_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "plans"
COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "allow_buy_extra",
        sa.Column("allow_buy_extra", sa.Boolean(), nullable=False, server_default=sa.false()),
    ),
    (
        "extra_gb_price",
        sa.Column("extra_gb_price", sa.Integer(), nullable=False, server_default="0"),
    ),
    (
        "extra_month_price",
        sa.Column("extra_month_price", sa.Integer(), nullable=False, server_default="0"),
    ),
)


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    existing = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for name, column in COLUMNS:
            if name not in existing:
                batch.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    existing = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for name, _column in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
