"""A photo or video on group messages: three columns on broadcasts.

Revision ID: 0137_broadcast_media
Revises: 0136_account_holds

``broadcast_logs.media_kind`` is ``photo`` or ``video`` (NULL for a text-only message),
``media_file_id`` the sending bot's Telegram file id once known, and ``media_file`` the stored
web upload the worker sends until it has that file id. Existing rows keep NULL and stay text-only.
Idempotent: each column is only added when missing. The downgrade drops the columns and keeps
every broadcast; rows that had media keep their text.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0137_broadcast_media"
down_revision: Union[str, None] = "0136_account_holds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BROADCASTS = "broadcast_logs"
COLUMNS: tuple[tuple[str, int], ...] = (
    ("media_kind", 8),
    ("media_file_id", 256),
    ("media_file", 64),
)


def _existing_columns() -> set[str] | None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(BROADCASTS):
        return None
    return {column["name"] for column in inspector.get_columns(BROADCASTS)}


def upgrade() -> None:
    existing = _existing_columns()
    if existing is None:
        return
    for name, length in COLUMNS:
        if name not in existing:
            op.add_column(BROADCASTS, sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    existing = _existing_columns() or set()
    for name, _length in reversed(COLUMNS):
        if name in existing:
            op.drop_column(BROADCASTS, name)
