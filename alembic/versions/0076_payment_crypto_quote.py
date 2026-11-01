"""The crypto amount a customer was quoted for a payment.

Revision ID: 0076_payment_crypto_quote
Revises: 0075_payment_receipt_at

``payments`` gains the asset, the amount (millionths of the asset), the rate (toman per unit x 10^4)
and the quote's expiry, so staff review a converted payment against what the customer was shown.
All four are nullable: a payment without a quote (conversion off, or older rows) keeps NULL.
Idempotent; the downgrade drops the columns and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0076_payment_crypto_quote"
down_revision: Union[str, None] = "0075_payment_receipt_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "payments"
COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("crypto_asset", sa.String(16)),
    ("crypto_amount_micro", sa.BigInteger()),
    ("crypto_rate_scaled", sa.BigInteger()),
    ("crypto_quote_expires_at", sa.DateTime(timezone=True)),
)


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    missing = [(name, kind) for name, kind in COLUMNS if name not in _columns(bind)]
    if missing:
        with op.batch_alter_table(TABLE) as batch:
            for name, kind in missing:
                batch.add_column(sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = [name for name, _ in COLUMNS if name in _columns(bind)]
    if present:
        with op.batch_alter_table(TABLE) as batch:
            for name in present:
                batch.drop_column(name)
