"""PAYG basis settings on reseller plans and the reseller charge ledger.

Revision ID: 0097_payg_charges
Revises: 0096_pg_panel_access

Adds the PAYG basis columns to ``reseller_plans``. Existing plans get basis ``traffic``
with charging at creation off, so every current PAYG shop keeps usage billing. Also
creates ``reseller_charges``. Idempotent; the downgrade drops only what this revision
added and keeps every other row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0097_payg_charges"
down_revision: Union[str, None] = "0096_pg_panel_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLANS = "reseller_plans"
CHARGES = "reseller_charges"
CHARGE_INDEXES = {
    "ix_reseller_charges_shop_request": ["shop_id", "request_key"],
    "ix_reseller_charges_shop_created": ["shop_id", "created_at"],
    "ix_reseller_charges_status": ["status"],
}


def _plan_columns() -> list[sa.Column]:
    return [
        sa.Column("payg_basis", sa.String(16), nullable=False, server_default=sa.text("'traffic'")),
        sa.Column("payg_charge_upfront", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payg_user_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("payg_user_price_per_30d", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payg_unlimited_price", sa.Integer(), nullable=True),
    ]


def _create_charges(bind: sa.Connection) -> None:
    references = [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("bot_users.id")]
    op.create_table(
        CHARGES,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("shop_id", sa.Integer(), *references, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("request_key", sa.String(160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("pg_username", sa.String(128), nullable=True),
        sa.Column("bytes_charged", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("days_charged", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("user_units", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("price_per_gb", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("user_price", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_unlimited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("panel_target", sa.Text(), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_reseller_charges_idempotency"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(PLANS):
        present = {c["name"] for c in inspector.get_columns(PLANS)}
        missing = [c for c in _plan_columns() if c.name not in present]
        if missing:
            with op.batch_alter_table(PLANS) as batch:
                for column in missing:
                    batch.add_column(column)
    if not inspector.has_table(CHARGES):
        _create_charges(bind)
    names = {i["name"] for i in sa.inspect(bind).get_indexes(CHARGES)}
    for name, columns in CHARGE_INDEXES.items():
        if name not in names:
            op.create_index(name, CHARGES, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(CHARGES):
        op.drop_table(CHARGES)
    if inspector.has_table(PLANS):
        present = {c["name"] for c in inspector.get_columns(PLANS)}
        names = [c.name for c in _plan_columns() if c.name in present]
        if names:
            with op.batch_alter_table(PLANS) as batch:
                for name in names:
                    batch.drop_column(name)
