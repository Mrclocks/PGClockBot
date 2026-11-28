"""Extra receipt photos of a payment.

Revision ID: 0144_payment_receipts
Revises: 0143_rich_descriptions

Adds ``payment_receipts``: photos a customer sends after the first receipt of the same pending payment
(the first stays ``payments.receipt_file_id``). ``forwarded_at`` records the one forward under the staff
review cards. A partial unique index keeps one row per photo and payment. Idempotent; the downgrade
drops the table (each payment keeps its first receipt).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0144_payment_receipts"
down_revision: Union[str, None] = "0143_rich_descriptions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "payment_receipts"
UNIQUE_FILE = "uq_payment_receipts_payment_unique_file"
BY_PAYMENT = "ix_payment_receipts_payment_id"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("file_id", sa.String(255), nullable=False),
            sa.Column("file_unique_id", sa.String(128), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("forwarded_at", sa.DateTime(timezone=True), nullable=True),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if BY_PAYMENT not in existing:
        op.create_index(BY_PAYMENT, TABLE, ["payment_id"])
    if UNIQUE_FILE not in existing:
        op.create_index(
            UNIQUE_FILE, TABLE, ["payment_id", "file_unique_id"], unique=True,
            postgresql_where=sa.text("file_unique_id IS NOT NULL"),
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
