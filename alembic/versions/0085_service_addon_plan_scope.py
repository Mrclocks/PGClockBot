"""Service addon packs: optional plan_id scope and button_style.

Revision ID: 0085_service_addon_plan_scope
Revises: 0084_reseller_plan_extra_packs

Adds ``plan_id`` (FK plans, CASCADE) and ``button_style`` on ``service_addon_packs``.
Idempotent on SQLite and PostgreSQL; downgrade drops the columns and keeps rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0085_service_addon_plan_scope"
down_revision: Union[str, None] = "0084_reseller_plan_extra_packs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "service_addon_packs"
PLAN_COL = "plan_id"
STYLE_COL = "button_style"
IX = "ix_service_addon_packs_plan_id"


def _columns(bind: sa.Connection) -> set[str]:
    if not sa.inspect(bind).has_table(TABLE):
        return set()
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind: sa.Connection) -> set[str]:
    if not sa.inspect(bind).has_table(TABLE):
        return set()
    return {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if not cols:
        return
    with op.batch_alter_table(TABLE) as batch:
        if PLAN_COL not in cols:
            batch.add_column(sa.Column(PLAN_COL, sa.Integer(), nullable=True))
        if STYLE_COL not in cols:
            batch.add_column(sa.Column(STYLE_COL, sa.String(length=16), nullable=True))
    cols = _columns(bind)
    # Named FK after the column exists (SQLite batch mode rejects anonymous FKs).
    if PLAN_COL in cols and sa.inspect(bind).has_table("plans"):
        fks = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys(TABLE) if fk.get("name")}
        fk_name = "fk_service_addon_packs_plan_id"
        if fk_name not in fks:
            with op.batch_alter_table(TABLE) as batch:
                batch.create_foreign_key(
                    fk_name, "plans", [PLAN_COL], ["id"], ondelete="CASCADE"
                )
    if PLAN_COL in cols and IX not in _indexes(bind):
        op.create_index(IX, TABLE, [PLAN_COL])


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if not cols:
        return
    if IX in _indexes(bind):
        op.drop_index(IX, table_name=TABLE)
    fk_name = "fk_service_addon_packs_plan_id"
    fks = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys(TABLE) if fk.get("name")}
    with op.batch_alter_table(TABLE) as batch:
        if fk_name in fks:
            batch.drop_constraint(fk_name, type_="foreignkey")
        if STYLE_COL in cols:
            batch.drop_column(STYLE_COL)
        if PLAN_COL in cols:
            batch.drop_column(PLAN_COL)
