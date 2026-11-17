"""Daily report accuracy: the first delivery time of an order and a Tehran send hour.

Revision ID: 0115_daily_report_accuracy
Revises: 0114_entry_approvals

``orders.delivered_at`` records when an order first became delivered, so a later edit of an old
order no longer counts it as delivered (and its amount as revenue) again. Existing delivered
orders take their ``updated_at``, the best time on record. The daily report hour was a UTC hour;
it becomes an Asia/Tehran hour, so stored hours move forward by four (UTC + 3:30, rounded up so
the reported day is complete when it is sent). Idempotent: the column and index are added only
when missing, and the hours are converted only once (a marker setting records it). The downgrade
moves the hours back and drops the index and the column; every other row is kept.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0115_daily_report_accuracy"
down_revision: Union[str, None] = "0114_entry_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "orders"
COLUMN = "delivered_at"
INDEX = "ix_orders_shop_delivered_at"
HOUR_KEY = "admin_daily_report_hour"
# Present once stored hours are Tehran hours; keeps a repeated upgrade from shifting them twice.
MARKER_KEY = "admin_daily_report_hour_zone"
MARKER_VALUE = "tehran"
TEHRAN_SHIFT_HOURS = 4


def _shift(raw: object, hours: int) -> str:
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        hour = 0
    return str((max(0, min(23, hour)) + hours) % 24)


def _shift_hours(bind: sa.engine.Connection, hours: int) -> None:
    settings = sa.table("settings", sa.column("id"), sa.column("key"), sa.column("value"))
    shops = sa.table("reseller_settings", sa.column("id"), sa.column("key"), sa.column("value"))
    for table in (settings, shops):
        rows = bind.execute(sa.select(table.c.id, table.c.value).where(table.c.key == HOUR_KEY)).all()
        for row_id, value in rows:
            bind.execute(sa.update(table).where(table.c.id == row_id).values(value=_shift(value, hours)))


def _hours_converted(bind: sa.engine.Connection) -> bool:
    settings = sa.table("settings", sa.column("key"), sa.column("value"))
    found = bind.execute(sa.select(settings.c.value).where(settings.c.key == MARKER_KEY)).first()
    return found is not None and found[0] == MARKER_VALUE


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if COLUMN not in {c["name"] for c in inspector.get_columns(TABLE)}:
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
        orders = sa.table(TABLE, sa.column("status"), sa.column("updated_at"), sa.column(COLUMN))
        bind.execute(
            sa.update(orders)
            .where(orders.c.status == "delivered", orders.c[COLUMN].is_(None))
            .values({COLUMN: orders.c.updated_at})
        )
    if INDEX not in {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, ["reseller_id", COLUMN])
    if inspector.has_table("settings") and inspector.has_table("reseller_settings") and not _hours_converted(bind):
        _shift_hours(bind, TEHRAN_SHIFT_HOURS)
        bind.execute(sa.text("INSERT INTO settings (key, value) VALUES (:k, :v)"), {"k": MARKER_KEY, "v": MARKER_VALUE})


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("settings") and inspector.has_table("reseller_settings") and _hours_converted(bind):
        _shift_hours(bind, -TEHRAN_SHIFT_HOURS)
        bind.execute(sa.text("DELETE FROM settings WHERE key = :k"), {"k": MARKER_KEY})
    if INDEX in {ix["name"] for ix in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
