"""Store external (panel-only) volume allocation on ResellerProfile.

Revision ID: 0066_external_volume_allocation
Revises: 0065_allow_volume_oversell

Users created directly in PasarGuard are not in UserService; a periodic job
sums their data_limit into ``external_allocated_bytes`` and stamps
``external_alloc_synced_at``. Idempotent; downgrade drops both columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0066_external_volume_allocation"
down_revision: Union[str, None] = "0065_allow_volume_oversell"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "reseller_profiles"
COLUMNS = (
    ("external_allocated_bytes", sa.BigInteger(), "0"),
    ("external_alloc_synced_at", sa.DateTime(timezone=True), None),
)


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    existing = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for name, col_type, default in COLUMNS:
            if name in existing:
                continue
            if default is not None:
                batch.add_column(
                    sa.Column(name, col_type, nullable=False, server_default=sa.text(default))
                )
            else:
                batch.add_column(sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    existing = _columns(bind)
    with op.batch_alter_table(TABLE) as batch:
        for name, _col_type, _default in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
