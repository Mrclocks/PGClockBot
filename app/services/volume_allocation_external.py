"""Count the volume of a shop's panel users that have no local service row.

Users made in PasarGuard outside a bot sale (panel pages, the panel itself) are not
``UserService`` rows. A background job lists each gated shop's panel users, sums the positive
limits of the ones without a local row, and stores the total on the shop profile, where
``volume_allocation`` counts it. A failed or incomplete listing keeps the previous total, so a
panel outage never frees volume. Each shop is read with its own short database sessions and
no transaction is open while the panel is called.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ResellerProfile, UserService
from app.py_compat import timeout as asyncio_timeout
from app.services.billing import BILLING_MODE_PAYG
from app.services.db_safe import rollback_quiet
from app.services.list_query import extract_list_total
from app.services.pasarguard import get_pg_for_reseller

logger = logging.getLogger(__name__)

SETTING_SYNC_MINUTES = "volume_external_sync_minutes"
DEFAULT_SYNC_MINUTES = 15
MAX_SYNC_MINUTES = 1440
# A count older than this many periods is shown as stale on the capacity page.
STALE_PERIODS = 2
PAGE_SIZE = 200
# A shop with more panel users than this is not counted (kept at its last total) and logged.
MAX_PANEL_USERS = 50_000
SHOP_TIMEOUT_SECONDS = 120.0
CONCURRENCY = 3
DUE_BATCH = 100
# Keeps IN lists well under the asyncpg and SQLite bound-parameter limits.
IN_CHUNK = 500


class PanelListingIncomplete(Exception):
    """The panel's user list could not be read in full; the old total stays."""


@dataclass(frozen=True, slots=True)
class ExternalSyncResult:
    shop_id: int
    external_bytes: int | None
    error: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sync_period_minutes(raw: str | None) -> int:
    try:
        minutes = int(str(raw or "").strip())
    except ValueError:
        return DEFAULT_SYNC_MINUTES
    return max(1, min(MAX_SYNC_MINUTES, minutes))


async def get_external_sync_minutes(session: AsyncSession) -> int:
    # Function-local: app.services.users imports the order flow, which imports the volume gate.
    from app.services.users import get_setting

    return sync_period_minutes(await get_setting(session, SETTING_SYNC_MINUTES, str(DEFAULT_SYNC_MINUTES)))


def external_sync_is_stale(synced_at: datetime | None, *, period_minutes: int, now: datetime | None = None) -> bool:
    if synced_at is None:
        return True
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    return (now or _utcnow()) - synced_at > timedelta(minutes=int(period_minutes) * STALE_PERIODS)


def panel_user_limit_bytes(user: dict[str, Any]) -> int:
    """Positive finite limit; unlimited (0) and missing count as zero, like local services."""
    raw = user.get("data_limit")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw > 0 else 0


def _page_users(data: Any) -> list[dict[str, Any]]:
    users = (data.get("users") or data.get("items") or []) if isinstance(data, dict) else data
    return [row for row in users if isinstance(row, dict)] if isinstance(users, list) else []


async def list_panel_users_for_admin(pg: Any, admin_username: str) -> list[dict[str, Any]]:
    """Every panel user owned by ``admin_username``; raise when the list cannot be read in full.

    Users created or deleted while paging can shift offsets, so the result is checked against
    the panel's own total and refused when they disagree.
    """
    by_id: dict[int, dict[str, Any]] = {}
    total: int | None = None
    offset = 0
    while offset <= MAX_PANEL_USERS:
        data = await pg.get_users(admin=admin_username, limit=PAGE_SIZE, offset=offset)
        if total is None:
            total = extract_list_total(data) if isinstance(data, dict) else None
        page = _page_users(data)
        for row in page:
            uid = row.get("id")
            if isinstance(uid, int) and not isinstance(uid, bool):
                by_id[uid] = row
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    else:
        raise PanelListingIncomplete("too many panel users")
    if total is not None and total != len(by_id):
        raise PanelListingIncomplete("panel list changed while paging")
    return list(by_id.values())


def _chunks(values: list[Any]) -> list[list[Any]]:
    return [values[i : i + IN_CHUNK] for i in range(0, len(values), IN_CHUNK)]


async def _local_matches(session: AsyncSession, shop_id: int, ids: list[int], names: list[str]) -> tuple[set[int], set[str]]:
    """Panel ids and lower-case usernames that the shop has as non-cancelled local services."""
    local_ids: set[int] = set()
    local_names: set[str] = set()
    live = (UserService.shop_id == int(shop_id), UserService.is_cancelled.is_(False))
    for chunk in _chunks(ids):
        rows = await session.scalars(select(UserService.pg_user_id).where(*live, UserService.pg_user_id.in_(chunk)))
        local_ids.update(int(x) for x in rows if x is not None)
    for chunk in _chunks(names):
        # Services saved before their panel id was known are matched by username.
        rows = await session.scalars(
            select(func.lower(UserService.pg_username)).where(*live, func.lower(UserService.pg_username).in_(chunk))
        )
        local_names.update(str(x) for x in rows if x)
    return local_ids, local_names


async def compute_external_bytes(session: AsyncSession, *, shop_id: int, panel_users: list[dict[str, Any]]) -> int:
    """Sum of limits of the panel users the shop has no local service for."""
    ids = [int(row["id"]) for row in panel_users if isinstance(row.get("id"), int)]
    names = sorted({str(row.get("username") or "").strip().lower() for row in panel_users} - {""})
    local_ids, local_names = await _local_matches(session, shop_id, ids, names)
    external = 0
    for row in panel_users:
        name = str(row.get("username") or "").strip().lower()
        if row.get("id") in local_ids or (name and name in local_names):
            continue
        external += panel_user_limit_bytes(row)
    return external


async def due_shop_ids(session: AsyncSession, *, period_minutes: int, limit: int = DUE_BATCH) -> list[int]:
    """Gated shops whose count is older than half a period, never-counted and oldest first."""
    cutoff = _utcnow() - timedelta(minutes=period_minutes) / 2
    synced = ResellerProfile.external_alloc_synced_at
    rows = await session.scalars(
        select(ResellerProfile.user_id)
        .where(
            ResellerProfile.is_active.is_(True),
            ResellerProfile.allow_volume_oversell.is_(False),
            func.lower(ResellerProfile.billing_mode) != BILLING_MODE_PAYG,
            ResellerProfile.pg_admin_username.is_not(None),
            or_(synced.is_(None), synced <= cutoff),
        )
        .order_by(synced.is_not(None), synced, ResellerProfile.user_id)
        .limit(int(limit))
    )
    return [int(x) for x in rows]


async def _read_panel_users(sessions: async_sessionmaker[AsyncSession], shop_id: int) -> list[dict[str, Any]] | None:
    async with sessions() as session:
        uname = (
            await session.scalar(select(ResellerProfile.pg_admin_username).where(ResellerProfile.user_id == shop_id))
            or ""
        ).strip()
        if not uname:
            return None
        pg = await get_pg_for_reseller(session, shop_id)
    # The session is closed here: nothing is held while the panel is paged.
    async with asyncio_timeout(SHOP_TIMEOUT_SECONDS):
        return await list_panel_users_for_admin(pg, uname)


async def sync_shop_external_allocation(
    sessions: async_sessionmaker[AsyncSession], shop_id: int
) -> ExternalSyncResult:
    """Recount one shop and store the total; on any failure keep the previous total."""
    try:
        panel_users = await _read_panel_users(sessions, shop_id)
    except Exception as exc:
        # Unreachable panel, bad credentials or a shifting list: the old total keeps counting.
        logger.warning("external volume sync: panel read failed shop=%s", shop_id, exc_info=True)
        return ExternalSyncResult(shop_id=shop_id, external_bytes=None, error=type(exc).__name__)
    if panel_users is None:
        return ExternalSyncResult(shop_id=shop_id, external_bytes=None, error="no_pg_admin")
    async with sessions() as session:
        try:
            external = await compute_external_bytes(session, shop_id=shop_id, panel_users=panel_users)
            await session.execute(
                update(ResellerProfile)
                .where(ResellerProfile.user_id == shop_id)
                .values(external_allocated_bytes=external, external_alloc_synced_at=_utcnow())
            )
            await session.commit()
        except SQLAlchemyError as exc:
            logger.warning("external volume sync: store failed shop=%s", shop_id, exc_info=True)
            await rollback_quiet(session)
            return ExternalSyncResult(shop_id=shop_id, external_bytes=None, error=type(exc).__name__)
    return ExternalSyncResult(shop_id=shop_id, external_bytes=external)


async def run_external_allocation_sync(sessions: async_sessionmaker[AsyncSession]) -> list[ExternalSyncResult]:
    """Recount the due shops, a few at a time; one failing shop never stops the others."""
    async with sessions() as session:
        period = await get_external_sync_minutes(session)
        shop_ids = await due_shop_ids(session, period_minutes=period)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(shop_id: int) -> ExternalSyncResult:
        async with gate:
            return await sync_shop_external_allocation(sessions, shop_id)

    return list(await asyncio.gather(*(one(sid) for sid in shop_ids)))
