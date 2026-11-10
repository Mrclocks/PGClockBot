"""Monthly credit statements of PAYG shops.

Revision ID: 0100_credit_statements
Revises: 0099_shop_credit

Creates ``credit_statements`` (unique per shop and month). Idempotent; the downgrade drops
only this table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0100_credit_statements"
down_revision: Union[str, None] = "0099_shop_credit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "credit_statements"
INDEXES = {"ix_credit_statements_shop_id": ["shop_id"], "ix_credit_statements_status": ["status"]}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        references = [] if bind.dialect.name == "sqlite" else [sa.ForeignKey("bot_users.id")]
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_id", sa.Integer(), *references, nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("amount_due", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("charges_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("payments_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'issued'")),
            sa.Column("notified_step", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("shop_id", "period_start", name="uq_credit_statement_period"),
        )
    names = {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}
    for name, columns in INDEXES.items():
        if name not in names:
            op.create_index(name, TABLE, columns)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
