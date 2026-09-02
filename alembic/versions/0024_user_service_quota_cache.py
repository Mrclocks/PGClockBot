"""Cache UserService.quota_* columns for users-list (avoid live PG fan-out).

Revision ID: 0024_user_service_quota_cache
Revises: 0023_lucky_wheel
Create Date: 2026-09-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_user_service_quota_cache"
down_revision: Union[str, None] = "0023_lucky_wheel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("user_services"):
        return
    cols = {c["name"] for c in insp.get_columns("user_services")}
    if "quota_expire_at" not in cols:
        op.add_column(
            "user_services",
            sa.Column("quota_expire_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "quota_data_limit_bytes" not in cols:
        op.add_column(
            "user_services",
            sa.Column("quota_data_limit_bytes", sa.BigInteger(), nullable=True),
        )
    if "quota_synced_at" not in cols:
        op.add_column(
            "user_services",
            sa.Column("quota_synced_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("user_services"):
        return
    cols = {c["name"] for c in insp.get_columns("user_services")}
    for name in ("quota_synced_at", "quota_data_limit_bytes", "quota_expire_at"):
        if name in cols:
            op.drop_column("user_services", name)
