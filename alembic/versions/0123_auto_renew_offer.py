"""Automatic-renewal offer: when it was made for a service.

Revision ID: 0123_auto_renew_offer
Revises: 0122_switch_reverts

Adds the nullable ``service_automations.renew_offered_at``. A service is offered automatic renewal
once, after its second manual renewal; the offer is claimed by setting this column where it is
still NULL. Idempotent; the downgrade drops the column and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0123_auto_renew_offer"
down_revision: Union[str, None] = "0122_switch_reverts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "service_automations"
COLUMN = "renew_offered_at"


def _has_column() -> bool:
    return COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    if not _has_column():
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
