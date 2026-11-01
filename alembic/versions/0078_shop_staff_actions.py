"""The shop staff action log.

Revision ID: 0078_shop_staff_actions
Revises: 0077_gift_codes_permission

``shop_staff_actions`` records who on a shop's staff did what (``shop_id`` NULL = platform).
Ids are plain integers without foreign keys, so a record outlives a deleted user or shop.
Reads are keyset-paginated per shop (``shop_id, id``). ``request_key`` is unique when set,
so a repeated request cannot write its record twice.

Idempotent; the downgrade drops the table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0078_shop_staff_actions"
down_revision: Union[str, None] = "0077_gift_codes_permission"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "shop_staff_actions"
SHOP_INDEX = "ix_shop_staff_actions_shop_id_id"
KEY_INDEX = "uq_shop_staff_actions_request_key"


def _indexes(bind: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shop_id", sa.Integer(), nullable=True),
            sa.Column("actor_kind", sa.String(16), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("actor_label", sa.String(128), nullable=False),
            sa.Column("action", sa.String(48), nullable=False),
            sa.Column("target_type", sa.String(32), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=True),
            sa.Column("amount", sa.BigInteger(), nullable=True),
            sa.Column("reason", sa.String(255), nullable=True),
            sa.Column("request_key", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    present = _indexes(bind)
    if SHOP_INDEX not in present:
        op.create_index(SHOP_INDEX, TABLE, ["shop_id", "id"])
    if KEY_INDEX not in present:
        op.create_index(
            KEY_INDEX,
            TABLE,
            ["request_key"],
            unique=True,
            sqlite_where=sa.text("request_key IS NOT NULL"),
            postgresql_where=sa.text("request_key IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    present = _indexes(bind)
    for name in (KEY_INDEX, SHOP_INDEX):
        if name in present:
            op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
