"""Remember the bot's tidy-up messages per chat in the database.

Revision ID: 0068_bot_chat_messages
Revises: 0067_settlement_next_check_at

Chat cleanup kept its list of deletable messages in memory only, so after a restart or an
update old welcomes and panels stayed in every chat for good. ``bot_chat_messages`` holds the
same list; rows older than Telegram's 48-hour delete window are purged by a job. Idempotent on
SQLite and PostgreSQL; the downgrade drops the table (it holds only cleanup hints).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0068_bot_chat_messages"
down_revision: Union[str, None] = "0067_settlement_next_check_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "bot_chat_messages"
SENT_INDEX = "ix_bot_chat_messages_sent_at"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("bot_id", sa.BigInteger(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("bot_id", "chat_id", "message_id", name="uq_bot_chat_messages_message"),
        )
    if SENT_INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(SENT_INDEX, TABLE, ["sent_at"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
