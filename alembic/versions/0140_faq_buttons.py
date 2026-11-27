"""Buttons under a frequently asked question's answer.

Revision ID: 0140_faq_buttons
Revises: 0139_start_watches

Adds ``faq_items.buttons`` (nullable text, the same JSON a group message stores for its buttons).
Existing questions keep NULL and show no extra buttons. Idempotent; the downgrade drops only the
column and keeps every question.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0140_faq_buttons"
down_revision: Union[str, None] = "0139_start_watches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "faq_items"
COLUMN = "buttons"


def _columns() -> set[str] | None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return None
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    columns = _columns()
    if columns is not None and COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    columns = _columns()
    if columns is not None and COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
