"""Schema-driven removal of every row that still references a bot user.

``delete_bot_user`` cleans known tables explicitly (it also has side effects such
as releasing gift discounts). This sweep runs afterwards as a safety net: it
walks the foreign keys declared in the models, so a table added later cannot
block the final ``DELETE FROM bot_users`` with an IntegrityError.

Policy per foreign key that points at a row being removed:
* nullable column on a shop-owned configuration table -> delete the row, because
  clearing its shop column would promote shop data to platform scope;
* any other nullable column (history, links, audit) -> set NULL;
* NOT NULL column -> delete the row, recursively removing what references it.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import ColumnElement, Table, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.db.models import BotUser
from app.services.customer_retention import assert_legacy_user_deletable

logger = logging.getLogger(__name__)

# Tables whose nullable shop column means "this row belongs to that shop".
SHOP_OWNED_TABLES: frozenset[str] = frozenset(
    {
        "catalog_discounts",
        "charge_codes",
        "loyalty_discount_entitlements",
        "loyalty_rewards",
        "lucky_wheel_prizes",
        "plan_categories",
        "plans",
        "points_rules",
        "service_addon_packs",
        "service_automations",
        "targeted_campaigns",
    }
)


@lru_cache(maxsize=1)
def _referencing_columns() -> dict[str, tuple[tuple[Table, str], ...]]:
    """Map table name -> (child table, column name) pairs that reference it."""
    found: dict[str, list[tuple[Table, str]]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                found.setdefault(foreign_key.column.table.name, []).append((table, column.name))
    return {name: tuple(pairs) for name, pairs in found.items()}


def _primary_key_column(table: Table) -> str | None:
    keys = list(table.primary_key.columns)
    return keys[0].name if len(keys) == 1 else None


async def _purge_children(
    session: AsyncSession,
    parent: Table,
    parent_where: ColumnElement[bool],
    *,
    stack: frozenset[tuple[str, str]],
) -> None:
    pk_name = _primary_key_column(parent)
    if pk_name is None:
        return
    parent_ids = select(parent.c[pk_name]).where(parent_where)
    for child, column_name in _referencing_columns().get(parent.name, ()):
        key = (child.name, column_name)
        if key in stack:
            continue
        column = child.c[column_name]
        child_where = column.in_(parent_ids)
        # A nullable channel adapter still owns legacy services. Clearing it
        # would violate canonical origin constraints and orphan the service.
        owns_legacy_service = key == ("user_services", "bot_user_id")
        if column.nullable and child.name not in SHOP_OWNED_TABLES and not owns_legacy_service:
            await session.execute(update(child).where(child_where).values({column_name: None}))
            continue
        await _purge_children(session, child, child_where, stack=stack | {key})
        await session.execute(delete(child).where(child_where))


async def purge_user_references(session: AsyncSession, user_id: int) -> None:
    """Remove or detach every row that still points at ``user_id``.

    The ``bot_users`` row itself is left for the caller to delete. The caller owns
    the transaction; nothing is committed here.
    """
    await assert_legacy_user_deletable(session, user_id)
    await _purge_children(
        session,
        BotUser.__table__,
        BotUser.__table__.c.id == user_id,
        stack=frozenset(),
    )
