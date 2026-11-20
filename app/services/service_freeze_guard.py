"""Refuse changes to a service while it is frozen.

While a service is paused its time is held in the freeze row, not on the panel. A renewal, an
add-on, an automatic purchase or a cancellation in that state would be computed from the panel's
stale expiry, and the resume would then overwrite it, so they wait until the customer resumes.
This module only reads the freeze table, so the order, renewal and cancellation services can
import it without importing the freeze service.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ServiceFreeze
from app.services.service_freeze_settings import OPEN_STATUSES

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
