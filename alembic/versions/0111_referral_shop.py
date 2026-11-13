"""Record the shop each referral was made in.

Revision ID: 0111_referral_shop
Revises: 0110_charge_code_plan_ids

Adds the nullable, indexed ``bot_users.referral_shop_id`` (NULL = the platform). Until now
a referral was rewarded by whichever shop the referred person bought from, so a customer
of a shop and of the platform earned the referrer points and bonuses on both sides.

Existing referrals are attributed to the referred person's shop when the referrer belongs
to that shop (its customer or its owner), which is the rule registration applies; every
other referral stays with the platform. Idempotent (only rows still NULL are filled, in
id batches); the downgrade drops the column and index and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0111_referral_shop"
down_revision: Union[str, None] = "0110_charge_code_plan_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "bot_users"
COLUMN = "referral_shop_id"
INDEX = "ix_bot_users_referral_shop_id"
BATCH_SIZE = 2000

# Fixed statement: only the id window is bound.
BACKFILL = sa.text(
    "UPDATE bot_users SET referral_shop_id = reseller_id "
    "WHERE id > :low AND id <= :high "
    "AND referred_by_id IS NOT NULL AND reseller_id IS NOT NULL AND referral_shop_id IS NULL "
    "AND (referred_by_id = reseller_id OR EXISTS ("
    "SELECT 1 FROM bot_users AS referrer "
    "WHERE referrer.id = bot_users.referred_by_id AND referrer.reseller_id = bot_users.reseller_id))"
)


def _has_column(bind: sa.Connection) -> bool:
    return COLUMN in {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def _has_index(bind: sa.Connection) -> bool:
    return INDEX in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}


def _backfill(bind: sa.Connection) -> None:
    top = int(bind.execute(sa.text("SELECT COALESCE(MAX(id), 0) FROM bot_users")).scalar() or 0)
    for low in range(0, top, BATCH_SIZE):
        bind.execute(BACKFILL, {"low": low, "high": min(low + BATCH_SIZE, top)})


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if not _has_column(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
    if not _has_index(bind):
        op.create_index(INDEX, TABLE, [COLUMN])
    _backfill(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if _has_index(bind):
        op.drop_index(INDEX, table_name=TABLE)
    if _has_column(bind):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
