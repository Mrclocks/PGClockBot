"""Add encrypted PasarGuard API key columns (admin X-Api-Key auth).

Revision ID: 0042_pg_api_key_enc
Revises: 0041_demo_users
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_pg_api_key_enc"
down_revision: Union[str, None] = "0041_demo_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "reseller_profiles",
    "pg_staff_access",
    "org_principals",
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in _TABLES:
        if not insp.has_table(table):
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "pg_api_key_enc" in cols:
            continue
        op.add_column(table, sa.Column("pg_api_key_enc", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in _TABLES:
        if not insp.has_table(table):
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "pg_api_key_enc" not in cols:
            continue
        op.drop_column(table, "pg_api_key_enc")
