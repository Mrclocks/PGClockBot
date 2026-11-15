"""PasarGuard panel access switch for reseller plans and shops.

Revision ID: 0096_pg_panel_access
Revises: 0095_broadcast_shop_id

Adds ``allow_pg_panel_access`` to ``reseller_plans`` and ``reseller_profiles`` (default
true, so every existing shop keeps its current access) and the nullable
``reseller_profiles.pg_password_rotation_enc`` that holds a new PasarGuard password
between saving it and the panel confirming it. Idempotent; the downgrade drops only the
new columns and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0096_pg_panel_access"
down_revision: Union[str, None] = "0095_broadcast_shop_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACCESS_COLUMN = "allow_pg_panel_access"
ROTATION_COLUMN = "pg_password_rotation_enc"
PLANS = "reseller_plans"
PROFILES = "reseller_profiles"


def _columns(bind: sa.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _access_column() -> sa.Column:
    return sa.Column(
        ACCESS_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("true")
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(PLANS) and ACCESS_COLUMN not in _columns(bind, PLANS):
        with op.batch_alter_table(PLANS) as batch:
            batch.add_column(_access_column())
    if inspector.has_table(PROFILES):
        present = _columns(bind, PROFILES)
        with op.batch_alter_table(PROFILES) as batch:
            if ACCESS_COLUMN not in present:
                batch.add_column(_access_column())
            if ROTATION_COLUMN not in present:
                batch.add_column(sa.Column(ROTATION_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(PROFILES):
        present = _columns(bind, PROFILES)
        with op.batch_alter_table(PROFILES) as batch:
            if ROTATION_COLUMN in present:
                batch.drop_column(ROTATION_COLUMN)
            if ACCESS_COLUMN in present:
                batch.drop_column(ACCESS_COLUMN)
    if inspector.has_table(PLANS) and ACCESS_COLUMN in _columns(bind, PLANS):
        with op.batch_alter_table(PLANS) as batch:
            batch.drop_column(ACCESS_COLUMN)
