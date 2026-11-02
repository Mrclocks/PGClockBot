"""Let a system alert replace the previous alert of the same kind in each chat.

Revision ID: 0069_bot_chat_message_replace_key
Revises: 0068_bot_chat_messages

``bot_chat_messages.replace_key`` names the alert («new version», capacity, volume, failed
deliveries) a stored message belongs to; sending the next one deletes or shortens the previous.
Idempotent on SQLite and PostgreSQL; the downgrade drops the column and index and keeps rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0069_bot_chat_message_replace_key"
down_revision: Union[str, None] = "0068_bot_chat_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "bot_chat_messages"
COLUMN = "replace_key"
INDEX = "ix_bot_chat_messages_replace"


def _columns(bind: sa.Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _indexes(bind: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if COLUMN not in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.String(64), nullable=True))
    if INDEX not in _indexes(bind):
        op.create_index(INDEX, TABLE, ["bot_id", COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if INDEX in _indexes(bind):
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
