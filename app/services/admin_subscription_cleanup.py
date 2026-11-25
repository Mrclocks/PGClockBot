"""Remove time/capacity subscription rows whose PasarGuard admin no longer exists.

A shop removed with its panel admin must not leave its subscription behind: the row would keep
counting as sold capacity and keep its expiry clock running for nobody. A row is deleted only when
no shop profile or staff login still points at the admin; rows kept by the owner for a plain panel
admin are deleted only after the panel confirms that admin is gone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PgAdminSubscription, PgStaffAccess, ResellerProfile
from app.services.pasarguard import PasarGuardError

logger = logging.getLogger(__name__)

# Leftover rows are few; this bounds one cleanup pass and the panel calls it makes.
CLEANUP_BATCH = 200
PANEL_CONCURRENCY = 4
PANEL_TIMEOUT_SECONDS = 15.0
ADMIN_MISSING = "missing"


def _unheld() -> Any:
    """No shop profile and no staff login refers to the subscription's admin."""
    held_by_shop = exists().where(ResellerProfile.pg_admin_username == PgAdminSubscription.pg_username)
    held_by_staff = exists().where(PgStaffAccess.pg_username == PgAdminSubscription.pg_username)
    return ~held_by_shop & ~held_by_staff


async def delete_unheld_subscription(session: AsyncSession, pg_username: str) -> bool:
    """Delete the admin's subscription when nothing holds it any more; the caller commits.

    One statement, so a shop or staff login linked to the admin in the meantime keeps the row.
    """
    uname = (pg_username or "").strip()
    if not uname:
        return False
    result = await session.execute(
        delete(PgAdminSubscription)
        .where(PgAdminSubscription.pg_username == uname, _unheld())
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def list_unheld_subscription_admins(session: AsyncSession, *, limit: int = CLEANUP_BATCH) -> list[str]:
    rows = await session.scalars(
        select(PgAdminSubscription.pg_username)
        .where(_unheld())
        .order_by(PgAdminSubscription.id)
        .limit(int(limit))
    )
    return [str(name) for name in rows if name]


async def _confirmed_missing(pg: Any, uname: str, gate: asyncio.Semaphore) -> bool:
    """True only on the panel's clean «not found»; an outage or a refusal keeps the row."""
    async with gate:
        try:
            state, _admin = await asyncio.wait_for(pg.get_admin_gate(uname), timeout=PANEL_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, PasarGuardError, httpx.HTTPError, ValueError):
            logger.warning("subscription cleanup: admin %s unreadable", uname, exc_info=True)
            return False
    return state == ADMIN_MISSING


async def prune_leftover_subscriptions(session_factory: Callable[[], Any], pg: Any) -> list[str]:
    """Delete unheld subscriptions whose panel admin is confirmed missing; return the names removed."""
    async with session_factory() as session:
        candidates = await list_unheld_subscription_admins(session)
    if not candidates:
        return []
    gate = asyncio.Semaphore(PANEL_CONCURRENCY)
    verdicts = await asyncio.gather(*(_confirmed_missing(pg, name, gate) for name in candidates))
    removed: list[str] = []
    for name, missing in zip(candidates, verdicts, strict=True):
        if not missing:
            continue
        async with session_factory() as session:
            if await delete_unheld_subscription(session, name):
                removed.append(name)
            await session.commit()
    return removed
