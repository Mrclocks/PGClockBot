"""Refund quote stored on each service cancellation request.

Revision ID: 0131_cancellation_refund_quote
Revises: 0130_discount_budget_winback

Adds nullable ``paid_amount``, ``remaining_percent`` and ``suggested_refund`` to
``service_cancellations``: the shop's refund rule applied when the request was made. Existing
requests keep NULL (no suggestion). Idempotent; the downgrade drops the three columns and keeps
every request.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0131_cancellation_refund_quote"
down_revision: Union[str, None] = "0130_discount_budget_winback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "service_cancellations"
COLUMNS: tuple[tuple[str, type[sa.types.TypeEngine]], ...] = (
    ("paid_amount", sa.BigInteger),
    ("remaining_percent", sa.Integer),
    ("suggested_refund", sa.BigInteger),
)


def _existing(bind: sa.engine.Connection) -> set[str] | None:
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return None
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing(op.get_bind())
    if existing is None:
        return
    missing = [(name, kind) for name, kind in COLUMNS if name not in existing]
    if not missing:
        return
    for name, kind in missing:
        op.add_column(TABLE, sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    existing = _existing(op.get_bind())
    if existing is None:
        return
    present = [name for name, _kind in COLUMNS if name in existing]
    if not present:
        return
    for name in present:
        op.drop_column(TABLE, name)
