"""Refuse changes to a service while it is frozen, and keep other paths from undoing a freeze.

While a service is paused its time is held in the freeze row, not on the panel. A renewal, an
add-on, an automatic purchase or a cancellation in that state would be computed from the panel's
stale expiry, and the resume would then overwrite it, so they wait until the customer resumes.
This module only reads the freeze table, so the order, renewal and cancellation services can
import it without importing the freeze service.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ServiceFreeze, UserService
from app.services.service_freeze_settings import OPEN_STATUSES, STATUS_CLOSED

FROZEN_REFUSAL = "این سرویس متوقف است؛ اول از «⚙️ بیشتر» آن را از حالت توقف خارج کنید"


async def is_frozen(session: AsyncSession, service_id: int | None) -> bool:
    if not service_id:
        return False
    found = await session.scalar(
        select(ServiceFreeze.id).where(
            ServiceFreeze.service_id == int(service_id), ServiceFreeze.status.in_(OPEN_STATUSES)
        ).limit(1)
    )
    return found is not None


async def assert_not_frozen(session: AsyncSession, service_id: int | None) -> None:
    """Raise the customer's message when the service has an open freeze."""
    if await is_frozen(session, service_id):
        raise ValueError(FROZEN_REFUSAL)


async def frozen_pg_user_ids(session: AsyncSession, pg_user_ids: Iterable[int]) -> set[int]:
    """Panel users among ``pg_user_ids`` whose service is frozen: a bulk re-enable must skip them."""
    ids = {int(i) for i in pg_user_ids}
    if not ids:
        return set()
    rows = await session.scalars(
        select(UserService.pg_user_id).join(ServiceFreeze, ServiceFreeze.service_id == UserService.id).where(
            UserService.pg_user_id.in_(ids), ServiceFreeze.status.in_(OPEN_STATUSES)
        )
    )
    return {int(i) for i in rows if i is not None}


async def close_freezes_of_pg_user(session: AsyncSession, pg_user_id: int) -> int:
    """Staff disabled this panel user on purpose: end its freeze so the deadline never re-enables it."""
    service_ids = select(UserService.id).where(UserService.pg_user_id == int(pg_user_id)).scalar_subquery()
    result = await session.execute(
        update(ServiceFreeze).where(ServiceFreeze.service_id.in_(service_ids), ServiceFreeze.status.in_(OPEN_STATUSES))
        .values(status=STATUS_CLOSED, resumed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return int(result.rowcount or 0)
