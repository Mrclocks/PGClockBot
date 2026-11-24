"""Group-message audiences with segments and filters: a column on broadcasts.

Revision ID: 0117_broadcast_audience_spec
Revises: 0116_message_buttons

``broadcast_logs.audience_spec`` holds the segment, filters and reference time of a broadcast as JSON
(``broadcast_audience.AudienceSpec``). Existing rows keep NULL and are sent to their ``audience`` key as before.
Idempotent: the column is only added when missing. The downgrade drops it; broadcasts are kept and fall back to
their ``audience`` key.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0117_broadcast_audience_spec"
down_revision: Union[str, None] = "0116_message_buttons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BROADCASTS = "broadcast_logs"
COLUMN = "audience_spec"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(BROADCASTS) and COLUMN in {c["name"] for c in inspector.get_columns(BROADCASTS)}


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(BROADCASTS) and not _has_column():
        with op.batch_alter_table(BROADCASTS) as batch:
            batch.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table(BROADCASTS) as batch:
            batch.drop_column(COLUMN)
