"""Premium emoji in plan and category descriptions.

Revision ID: 0143_rich_descriptions
Revises: 0142_service_link_resets

Adds nullable ``description_rich`` to ``plans`` and ``plan_categories``: the description with its
premium emoji when it is edited in the bot. ``description`` stays the plain text every other reader
uses. Idempotent; the downgrade drops the two columns and keeps every description.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0143_rich_descriptions"
down_revision: Union[str, None] = "0142_service_link_resets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("plans", "plan_categories")
COLUMN = "description_rich"


def _has_column(table: str) -> bool:
    return any(col["name"] == COLUMN for col in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    for table in TABLES:
        if not _has_column(table):
            op.add_column(table, sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        if _has_column(table):
            op.drop_column(table, COLUMN)
