"""Transactional legacy adapters; never infer a shop or reactivate a blocked identity."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer, new_customer_public_id
from app.db.models import BotUser


LEGACY_IDENTITY_BATCH_SIZE = 500


async def ensure_legacy_customers(session: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    """Batch within the caller's transaction; constraints resolve duplicate adapters."""
    if not user_ids or len(user_ids) > LEGACY_IDENTITY_BATCH_SIZE:
        raise ValueError("Legacy adapter batch must contain 1 to 500 users")
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(update(BotUser).where(BotUser.id == min(user_ids)).values(id=BotUser.id))
    users = list((await session.execute(select(BotUser.id, BotUser.is_blocked).where(
        BotUser.id.in_(user_ids),
    ).order_by(BotUser.id).with_for_update())).all())
    if len(users) != len(set(user_ids)):
        raise ValueError("Legacy customer owner is missing")
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    await session.execute(insert(Customer).on_conflict_do_nothing(index_elements=["legacy_bot_user_id"]), [
        {"public_id": new_customer_public_id(), "legacy_bot_user_id": user.id,
         "state": "blocked" if user.is_blocked else "active"} for user in users
    ])
    blocked_ids = [user.id for user in users if user.is_blocked]
    if blocked_ids:
        await session.execute(update(Customer).where(Customer.legacy_bot_user_id.in_(blocked_ids)).values(state="blocked"))
    return dict((await session.execute(select(Customer.legacy_bot_user_id, Customer.id).where(
        Customer.legacy_bot_user_id.in_(user_ids),
    ))).all())


async def ensure_legacy_customer(session: AsyncSession, bot_user_id: int) -> int:
    return (await ensure_legacy_customers(session, [bot_user_id]))[bot_user_id]


async def set_legacy_customer_blocked(session: AsyncSession, user: BotUser, *, blocked: bool) -> None:
    """Caller authorizes the legacy account; synchronize its adapter in the same unit of work."""
    if type(blocked) is not bool:
        raise ValueError("وضعیت مسدودی معتبر نیست")
    user.is_blocked = blocked
    await session.flush()
    customer_id = await ensure_legacy_customer(session, user.id)
    await session.execute(update(Customer).where(
        Customer.id == customer_id, Customer.legacy_bot_user_id == user.id,
    ).values(state="blocked" if blocked else "active"))
