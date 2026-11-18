"""Usage at the start of a service's current period.

Revision ID: 0119_service_period_baseline
Revises: 0118_shop_targeted_message_days

Adds nullable ``period_used_baseline_bytes`` and ``period_lifetime_baseline_bytes`` to
``user_services``. A renewal stores the panel usage at the moment it opened the period, so
the bot shows and alerts on this period's volume instead of the lifetime total. Existing rows
stay NULL and keep showing the raw panel numbers until their next renewal.

Idempotent (columns are only added when missing); the downgrade drops the two columns and
keeps every service row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0119_service_period_baseline"
down_revision: Union[str, None] = "0118_shop_targeted_message_days"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "user_services"
COLUMNS = ("period_used_baseline_bytes", "period_lifetime_baseline_bytes")


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    missing = [name for name in COLUMNS if name not in _columns(bind)]
    if not missing:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in missing:
            batch.add_column(sa.Column(name, sa.BigInteger(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = [name for name in COLUMNS if name in _columns(bind)]
    if not present:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in present:
            batch.drop_column(name)
