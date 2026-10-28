"""Unresolved panel effects retain capacity independently of leases and temporary holds."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_operation_models import IntegrationCapacityCommitment


async def durable_user_slots(session: AsyncSession, *, shop_id: int | None, pg_username: str | None = None) -> int:
    value = await session.scalar(select(func.coalesce(func.sum(IntegrationCapacityCommitment.user_slots), 0)).where(
        or_(IntegrationCapacityCommitment.shop_id == shop_id,
            IntegrationCapacityCommitment.pg_username == pg_username.strip().lower() if pg_username else False),
        IntegrationCapacityCommitment.active.is_(True),
    ))
    return int(value or 0)
