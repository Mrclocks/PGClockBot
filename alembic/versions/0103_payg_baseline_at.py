"""Mark when panel reconciliation took a shop's baseline.

Revision ID: 0103_payg_baseline_at
Revises: 0102_child_pricing

Adds nullable ``reseller_profiles.payg_baseline_at``. A shop whose first run saw no
panel users used to get no baseline, so its first users were later taken as baseline
for free. Idempotent; the downgrade drops only this column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0103_payg_baseline_at"
down_revision: Union[str, None] = "0102_child_pricing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "reseller_profiles"
COLUMN = "payg_baseline_at"


def _has_column(bind: sa.Connection) -> bool:
    inspector = sa.inspect(bind)
    return inspector.has_table(TABLE) and COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE) and not _has_column(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column(op.get_bind()):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
