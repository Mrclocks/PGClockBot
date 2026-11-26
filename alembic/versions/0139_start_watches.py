"""Staff can ask to hear when a person first starts the bot.

Revision ID: 0139_start_watches
Revises: 0138_faq_items

Adds ``start_watches``: one row per watcher and target (``id:<telegram id>`` or ``u:<username>``),
open until ``notified_at`` is set or ``expires_at`` passes. A partial unique index keeps one open
watch per watcher and target. Idempotent; the downgrade drops only this new table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0139_start_watches"
down_revision: Union[str, None] = "0138_faq_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "start_watches"
OPEN = sa.text("notified_at IS NULL")
INDEXES: tuple[tuple[str, tuple[str, ...], dict], ...] = (
    ("uq_start_watches_open", ("watcher_telegram_id", "target"), {"unique": True, "postgresql_where": OPEN}),
    ("ix_start_watches_open_expiry", ("expires_at",), {"postgresql_where": OPEN}),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("watcher_telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("target", sa.String(48), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        )
        inspector = sa.inspect(op.get_bind())
    existing = {index["name"] for index in inspector.get_indexes(TABLE)}
    for name, columns, options in INDEXES:
        if name not in existing:
            op.create_index(name, TABLE, list(columns), **options)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
