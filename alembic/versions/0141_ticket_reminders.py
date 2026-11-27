"""When staff were last reminded about an unanswered ticket.

Revision ID: 0141_ticket_reminders
Revises: 0140_faq_buttons

Adds ``tickets.reminded_at`` (nullable, timezone-aware). The reminder job claims a ticket by
setting it, so one waiting period is reminded once even when two workers run. Existing tickets
keep NULL. Idempotent; the downgrade drops only the column and keeps every ticket.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0141_ticket_reminders"
down_revision: Union[str, None] = "0140_faq_buttons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "tickets"
COLUMN = "reminded_at"


def _columns() -> set[str] | None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return None
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    columns = _columns()
    if columns is not None and COLUMN not in columns:
        op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    columns = _columns()
    if columns is not None and COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
