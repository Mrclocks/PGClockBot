"""Each shop keeps its own private note and risk tag on a customer.

Revision ID: 0083_shop_customer_marks
Revises: 0082_shop_broad_campaign_days

``bot_user_shops`` gains ``staff_note`` and ``color_tag``. Until now a shop's marks lived on
``bot_users`` (one record per person), readable wherever the person appeared. For every
person attributed to a shop, the marks move to that shop's membership row (created when
missing) and are cleared from ``bot_users``; platform customers keep theirs on ``bot_users``.

Idempotent (only rows whose marks are still on ``bot_users`` are moved; keyset batches).
The downgrade copies the attributing shop's marks back to ``bot_users``, then drops the two
columns and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0083_shop_customer_marks"
down_revision: Union[str, None] = "0082_shop_broad_campaign_days"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MEMBERS = "bot_user_shops"
USERS = "bot_users"
COLUMNS = ("staff_note", "color_tag")
DEFAULT_TAG = "green"
BATCH = 500

users = sa.table(
    USERS,
    sa.column("id", sa.Integer),
    sa.column("reseller_id", sa.Integer),
    sa.column("staff_note", sa.Text),
    sa.column("color_tag", sa.String),
)
members = sa.table(
    MEMBERS,
    sa.column("id", sa.Integer),
    sa.column("bot_user_id", sa.Integer),
    sa.column("shop_id", sa.Integer),
    sa.column("staff_note", sa.Text),
    sa.column("color_tag", sa.String),
)


def _columns(bind: sa.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _has_marks() -> sa.ColumnElement[bool]:
    return sa.or_(
        sa.and_(users.c.staff_note.is_not(None), users.c.staff_note != ""),
        sa.and_(users.c.color_tag.is_not(None), users.c.color_tag.not_in(("", DEFAULT_TAG))),
    )


def _move_to_members(bind: sa.Connection) -> None:
    last_id = 0
    while True:
        rows = bind.execute(
            sa.select(users.c.id, users.c.reseller_id, users.c.staff_note, users.c.color_tag)
            .where(users.c.id > last_id, users.c.reseller_id.is_not(None), _has_marks())
            .order_by(users.c.id)
            .limit(BATCH)
        ).all()
        if not rows:
            return
        for row in rows:
            membership = sa.and_(members.c.bot_user_id == row.id, members.c.shop_id == row.reseller_id)
            if bind.execute(sa.select(members.c.id).where(membership)).first() is None:
                bind.execute(sa.insert(members).values(bot_user_id=row.id, shop_id=row.reseller_id))
            bind.execute(sa.update(members).where(membership).values(staff_note=row.staff_note, color_tag=row.color_tag))
            bind.execute(sa.update(users).where(users.c.id == row.id).values(staff_note=None, color_tag=DEFAULT_TAG))
        last_id = rows[-1].id


def _copy_back(bind: sa.Connection) -> None:
    attributed = sa.and_(members.c.bot_user_id == users.c.id, members.c.shop_id == users.c.reseller_id)
    for name in COLUMNS:
        value = sa.select(members.c[name]).where(attributed).scalar_subquery()
        has_value = sa.exists(sa.select(members.c.id).where(attributed, members.c[name].is_not(None)))
        bind.execute(sa.update(users).where(users.c.reseller_id.is_not(None), has_value).values({name: value}))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not (inspector.has_table(MEMBERS) and inspector.has_table(USERS)):
        return
    present = _columns(bind, MEMBERS)
    # Plain ADD COLUMN: no table rebuild, so the partial unique indexes stay.
    if "staff_note" not in present:
        op.add_column(MEMBERS, sa.Column("staff_note", sa.Text(), nullable=True))
    if "color_tag" not in present:
        op.add_column(MEMBERS, sa.Column("color_tag", sa.String(16), nullable=True))
    _move_to_members(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(MEMBERS):
        return
    present = [name for name in COLUMNS if name in _columns(bind, MEMBERS)]
    if len(present) == len(COLUMNS):
        _copy_back(bind)
    if present:
        with op.batch_alter_table(MEMBERS) as batch:
            for name in present:
                batch.drop_column(name)
