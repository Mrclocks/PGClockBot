"""Resumable identity expansion in bounded, caller-committed keyset batches."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer, new_service_public_id
from app.db.models import BotUser, UserService
from app.services.customer_identity import LEGACY_IDENTITY_BATCH_SIZE, ensure_legacy_customers

BACKFILL_BATCH_SIZE = LEGACY_IDENTITY_BATCH_SIZE
MAX_BACKFILL_CURSOR = 2**31 - 1


@dataclass(frozen=True, slots=True)
class IdentityBatch:
    last_id: int
    scanned: int


def validate_cursor(after: int, limit: int) -> None:
    if type(after) is not int or not 0 <= after <= MAX_BACKFILL_CURSOR:
        raise ValueError("Invalid backfill cursor")
    if type(limit) is not int or not 1 <= limit <= BACKFILL_BATCH_SIZE:
        raise ValueError("Invalid backfill batch size")


async def backfill_legacy_customers(
    session: AsyncSession, *, after: int = 0, limit: int = BACKFILL_BATCH_SIZE,
) -> IdentityBatch:
    validate_cursor(after, limit)
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(update(BotUser).where(BotUser.id == after).values(id=BotUser.id))
    ids = list(await session.scalars(select(BotUser.id).where(
        BotUser.id > after,
    ).order_by(BotUser.id).limit(limit)))
    if ids:
        await ensure_legacy_customers(session, ids)
    return IdentityBatch(ids[-1] if ids else after, len(ids))


async def backfill_legacy_services(
    session: AsyncSession, *, after: int = 0, limit: int = BACKFILL_BATCH_SIZE,
) -> IdentityBatch:
    validate_cursor(after, limit)
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(update(UserService).where(UserService.id == after).values(id=UserService.id))
    # Lock only the service rows on PostgreSQL; an outer join cannot lock its nullable side.
    rows = list((await session.execute(select(
        UserService.id, UserService.bot_user_id, UserService.customer_id, UserService.public_id,
        Customer.id.label("canonical_id"),
    ).outerjoin(Customer, Customer.legacy_bot_user_id == UserService.bot_user_id).where(
        UserService.id > after, UserService.bot_user_id.is_not(None),
    ).order_by(UserService.id).limit(limit).with_for_update(of=UserService))).all())
    if any(row.canonical_id is None or row.customer_id not in (None, row.canonical_id) for row in rows):
        raise ValueError("Legacy service identity needs review; batch was not changed")
    if rows:
        table = UserService.__table__
        connection = await session.connection()
        await connection.execute(update(table).where(
            table.c.id == bindparam("row_id"), table.c.bot_user_id == bindparam("owner_id"),
            table.c.customer_id.is_(None) | (table.c.customer_id == bindparam("canonical_id")),
        ).values(customer_id=bindparam("canonical_id"), public_id=bindparam("stable_public_id")), [
            {"row_id": row.id, "owner_id": row.bot_user_id, "canonical_id": row.canonical_id,
             "stable_public_id": row.public_id or new_service_public_id()} for row in rows
        ])
    return IdentityBatch(rows[-1].id if rows else after, len(rows))
