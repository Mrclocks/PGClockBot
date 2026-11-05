"""Per-plan reseller quick-buy stairs (extra GB / users) with button style.

Revision ID: 0084_reseller_plan_extra_packs
Revises: 0083_shop_customer_marks

Creates ``reseller_plan_extra_packs`` and seeds the historic defaults
(1/5/10/50 GB and 1/5/10/20 users) for subscription plans that already have
``allow_buy_extra`` on. Idempotent on SQLite and PostgreSQL; downgrade drops
the table and keeps plan rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0084_reseller_plan_extra_packs"
down_revision: Union[str, None] = "0083_shop_customer_marks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "reseller_plan_extra_packs"
PLANS = "reseller_plans"
UQ = "uq_reseller_plan_extra_pack_amount"
IX = "ix_reseller_plan_extra_packs_plan_kind"

DEFAULT_GB = (1, 5, 10, 50)
DEFAULT_USERS = (1, 5, 10, 20)


def _has_table(bind: sa.Connection, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _index_names(bind: sa.Connection, table: str) -> set[str]:
    if not _has_table(bind, table):
        return set()
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}


def _create_table(bind: sa.Connection) -> None:
    if _has_table(bind, TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("reseller_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("button_style", sa.String(length=16), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("plan_id", "kind", "amount", name=UQ),
    )
    if IX not in _index_names(bind, TABLE):
        op.create_index(IX, TABLE, ["plan_id", "kind"])


def _seed_defaults(bind: sa.Connection) -> None:
    if not _has_table(bind, TABLE) or not _has_table(bind, PLANS):
        return
    plans = sa.table(
        PLANS,
        sa.column("id", sa.Integer),
        sa.column("allow_buy_extra", sa.Boolean),
    )
    packs = sa.table(
        TABLE,
        sa.column("id", sa.Integer),
        sa.column("plan_id", sa.Integer),
        sa.column("kind", sa.String),
        sa.column("amount", sa.Integer),
        sa.column("button_style", sa.String),
        sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )
    # Plans that already allow extras and still have zero pack rows.
    plan_ids = [
        int(row[0])
        for row in bind.execute(
            sa.select(plans.c.id).where(plans.c.allow_buy_extra.is_(True))
        ).fetchall()
    ]
    if not plan_ids:
        return
    existing = {
        (int(r[0]), str(r[1]), int(r[2]))
        for r in bind.execute(
            sa.select(packs.c.plan_id, packs.c.kind, packs.c.amount).where(
                packs.c.plan_id.in_(plan_ids)
            )
        ).fetchall()
    }
    rows: list[dict] = []
    for plan_id in plan_ids:
        if any(pid == plan_id for pid, _kind, _amt in existing):
            continue
        for kind, amounts in (("gb", DEFAULT_GB), ("users", DEFAULT_USERS)):
            for index, amount in enumerate(amounts):
                key = (plan_id, kind, amount)
                if key in existing:
                    continue
                rows.append(
                    {
                        "plan_id": plan_id,
                        "kind": kind,
                        "amount": amount,
                        "button_style": None,
                        "sort_order": index,
                        "is_active": True,
                    }
                )
    if rows:
        op.bulk_insert(packs, rows)


def upgrade() -> None:
    bind = op.get_bind()
    _create_table(bind)
    # Re-check index if table pre-existed without it.
    if _has_table(bind, TABLE) and IX not in _index_names(bind, TABLE):
        op.create_index(IX, TABLE, ["plan_id", "kind"])
    _seed_defaults(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, TABLE):
        op.drop_table(TABLE)
