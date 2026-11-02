"""A shop can block one of its customers in that shop only.

Revision ID: 0081_shop_customer_block
Revises: 0080_shop_points_adjust_days

``bot_user_shops`` gains ``is_blocked`` (NOT NULL, default false), ``blocked_at`` and
``blocked_reason``. Existing memberships stay unblocked. The account-wide
``bot_users.is_blocked`` is unchanged.

Idempotent; the downgrade drops the three columns and keeps every membership row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0081_shop_customer_block"
down_revision: Union[str, None] = "0080_shop_points_adjust_days"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "bot_user_shops"
COLUMNS = ("is_blocked", "blocked_at", "blocked_reason")


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _definition(name: str) -> sa.Column:
    if name == "is_blocked":
        return sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false())
    if name == "blocked_at":
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.String(255), nullable=True)


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    # Plain ADD COLUMN on both dialects: no table rebuild, so the partial unique indexes stay.
    for name in COLUMNS:
        if name not in _columns(bind):
            op.add_column(TABLE, _definition(name))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = [name for name in COLUMNS if name in _columns(bind)]
    if present:
        with op.batch_alter_table(TABLE) as batch:
            for name in present:
                batch.drop_column(name)
