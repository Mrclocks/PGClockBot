"""Shop service control gets its own permission key, ``customer_services``.

Revision ID: 0088_customer_services_permission
Revises: 0087_reseller_plan_carry_on_renew

Bot and web staff who already had ``dashboard`` (the previous gate for the users
list) keep access to live service info/edit by gaining ``customer_services``.
``NULL`` lists still resolve to the defaults (which now include the key). Empty
lists stay empty (explicit deny). Upgrade is idempotent; downgrade strips the key.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0088_customer_services_permission"
down_revision: Union[str, None] = "0087_reseller_plan_carry_on_renew"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "customer_services"
GRANTING_KEYS = frozenset({"dashboard"})
TABLES = ("reseller_profiles", "reseller_plans")
COLUMNS = ("web_permissions", "bot_permissions")
BATCH = 500


def _keys(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def with_customer_services(raw: str | None) -> str | None:
    if raw is None:
        return None
    keys = _keys(raw)
    if KEY in keys or not GRANTING_KEYS.intersection(keys):
        return raw
    return ",".join([*keys, KEY])


def without_customer_services(raw: str | None) -> str | None:
    if raw is None:
        return None
    keys = _keys(raw)
    if KEY not in keys:
        return raw
    return ",".join(key for key in keys if key != KEY)


def _rewrite(bind: sa.Connection, change) -> None:
    inspector = sa.inspect(bind)
    for table_name in TABLES:
        if not inspector.has_table(table_name):
            continue
        present = {column["name"] for column in inspector.get_columns(table_name)}
        columns = [name for name in COLUMNS if name in present]
        if not columns:
            continue
        table = sa.table(
            table_name,
            sa.column("id", sa.Integer),
            *(sa.column(name, sa.Text) for name in columns),
        )
        last_id = 0
        while True:
            rows = bind.execute(
                sa.select(table.c.id, *(table.c[name] for name in columns))
                .where(table.c.id > last_id)
                .order_by(table.c.id)
                .limit(BATCH)
            ).all()
            if not rows:
                break
            for row in rows:
                values = {name: change(row._mapping[name]) for name in columns}
                updates = {
                    name: value
                    for name, value in values.items()
                    if value != row._mapping[name]
                }
                if updates:
                    bind.execute(
                        sa.update(table).where(table.c.id == row.id).values(**updates)
                    )
            last_id = rows[-1].id


def upgrade() -> None:
    _rewrite(op.get_bind(), with_customer_services)


def downgrade() -> None:
    _rewrite(op.get_bind(), without_customer_services)
