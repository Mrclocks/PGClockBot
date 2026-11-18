"""Separate renewal carry-over switches for volume and time (customer plans) and for extra
GB and extra users (reseller packages).

Revision ID: 0121_split_renewal_carry
Revises: 0120_service_period_baseline

Adds ``plans.carry_remaining_volume`` / ``plans.carry_remaining_time`` and
``reseller_plans.keep_extra_gb_on_renew`` / ``reseller_plans.keep_extra_users_on_renew``
(NOT NULL, server default true). Each new pair is filled from the combined switch it replaces
(``carry_remaining_quota``, ``keep_extras_on_renew``) only when the columns are added, so every
plan keeps its behavior. The old columns stay; a later release drops them.

Idempotent; the downgrade drops the new columns and keeps every plan row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0121_split_renewal_carry"
down_revision: Union[str, None] = "0120_service_period_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# table -> (combined switch it replaces, new columns); identifiers are fixed here, never input.
SPLITS = {
    "plans": ("carry_remaining_quota", ("carry_remaining_volume", "carry_remaining_time")),
    "reseller_plans": ("keep_extras_on_renew", ("keep_extra_gb_on_renew", "keep_extra_users_on_renew")),
}


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table, (source, targets) in SPLITS.items():
        if not sa.inspect(bind).has_table(table):
            continue
        existing = _columns(bind, table)
        missing = [name for name in targets if name not in existing]
        if not missing:
            continue
        with op.batch_alter_table(table) as batch:
            for name in missing:
                batch.add_column(sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))
        if source in existing:
            for name in missing:
                op.execute(sa.text(f"UPDATE {table} SET {name} = {source} WHERE {source} IS NOT NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    for table, (_source, targets) in SPLITS.items():
        if not sa.inspect(bind).has_table(table):
            continue
        present = [name for name in targets if name in _columns(bind, table)]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for name in present:
                batch.drop_column(name)
