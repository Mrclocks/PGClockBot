"""Gift codes get their own shop permission key, ``gift_codes``.

Revision ID: 0077_gift_codes_permission
Revises: 0076_payment_crypto_quote

Until now a shop could create, edit and toggle gift codes with the ``orders`` or ``plans``
permission. Gift codes mint wallet credit, so they now need ``gift_codes``. To keep every
shop's current access, each saved permission list (reseller profiles and reseller packages,
web and mirrored bot column) that holds ``orders`` or ``plans`` gains ``gift_codes``.

Left as they are: ``NULL`` lists (they resolve to the defaults, which now include the key)
and empty lists (an owner's explicit "no permissions"). Running the upgrade twice changes
nothing. The downgrade removes the key from every list and keeps every row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0077_gift_codes_permission"
down_revision: Union[str, None] = "0076_payment_crypto_quote"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KEY = "gift_codes"
GRANTING_KEYS = frozenset({"orders", "plans"})
# Fixed in code; never built from input.
TABLES = ("reseller_profiles", "reseller_plans")
COLUMNS = ("web_permissions", "bot_permissions")
BATCH = 500


def _keys(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def with_gift_codes(raw: str | None) -> str | None:
    """The list with ``gift_codes`` added when it grants orders or plans; else unchanged."""
    if raw is None:
        return None
    keys = _keys(raw)
    if KEY in keys or not GRANTING_KEYS.intersection(keys):
        return raw
    return ",".join([*keys, KEY])


def without_gift_codes(raw: str | None) -> str | None:
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
        table = sa.table(table_name, sa.column("id", sa.Integer), *(sa.column(name, sa.Text) for name in columns))
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
                updates = {name: value for name, value in values.items() if value != row._mapping[name]}
                if updates:
                    bind.execute(sa.update(table).where(table.c.id == row.id).values(**updates))
            last_id = rows[-1].id


def upgrade() -> None:
    _rewrite(op.get_bind(), with_gift_codes)


def downgrade() -> None:
    _rewrite(op.get_bind(), without_gift_codes)
