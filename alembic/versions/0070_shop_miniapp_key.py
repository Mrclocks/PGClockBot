"""Random public key for a shop's Mini App address.

Revision ID: 0070_shop_miniapp_key
Revises: 0069_bot_chat_message_replace_key

A shop's Mini App is served at an address carrying this key; the shop is found by it and
every request is still verified with that shop bot's own token. Existing shops start with
no key (Mini App off). Idempotent; the downgrade drops the index and column and keeps rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0070_shop_miniapp_key"
down_revision: Union[str, None] = "0069_bot_chat_message_replace_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "reseller_profiles"
COLUMN = "miniapp_key"
INDEX = "ix_reseller_profiles_miniapp_key"


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
            batch.add_column(sa.Column(COLUMN, sa.String(32), nullable=True))
    if INDEX not in _indexes(bind):
        op.create_index(INDEX, TABLE, [COLUMN], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if INDEX in _indexes(bind):
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _columns(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
