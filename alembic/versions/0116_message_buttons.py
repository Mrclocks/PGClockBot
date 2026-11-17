"""Buttons under staff messages: a column on broadcasts and a table of saved button sets.

Revision ID: 0116_message_buttons
Revises: 0115_daily_report_accuracy

``broadcast_logs.buttons`` holds the buttons of a queued broadcast (NULL = none, every existing row).
``message_button_presets`` holds each shop's saved sets (``shop_key`` 0 = platform), one name per shop.
Idempotent: the column, table and index are only added when missing. The downgrade drops them; the
broadcasts themselves are kept, only without their buttons.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0116_message_buttons"
down_revision: Union[str, None] = "0115_daily_report_accuracy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BROADCASTS = "broadcast_logs"
COLUMN = "buttons"
PRESETS = "message_button_presets"
SHOP_INDEX = "ix_message_button_presets_shop_key"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(BROADCASTS) and COLUMN not in {c["name"] for c in inspector.get_columns(BROADCASTS)}:
        with op.batch_alter_table(BROADCASTS) as batch:
            batch.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))
    if not inspector.has_table(PRESETS):
        op.create_table(
            PRESETS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_key", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("name", sa.String(40), nullable=False),
            sa.Column("buttons", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("shop_key", "name", name="uq_message_button_presets_shop_name"),
        )
    if SHOP_INDEX not in {ix["name"] for ix in sa.inspect(bind).get_indexes(PRESETS)}:
        op.create_index(SHOP_INDEX, PRESETS, ["shop_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(PRESETS):
        op.drop_table(PRESETS)
    if inspector.has_table(BROADCASTS) and COLUMN in {c["name"] for c in inspector.get_columns(BROADCASTS)}:
        with op.batch_alter_table(BROADCASTS) as batch:
            batch.drop_column(COLUMN)
