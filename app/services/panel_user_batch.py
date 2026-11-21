"""Read the live panel state of many services with few PasarGuard calls.

Background jobs (usage alerts, alert re-arm, first-use nudge) used to read every service with
its own subscription lookup. The admin user list accepts an ``ids`` filter, so one call returns
up to ``ID_CHUNK`` users with the same fields (status, expire, used_traffic, data_limit,
lifetime_used_traffic) as the subscription response.

A row is used only when its id is the service's ``pg_user_id`` and its username matches the
service's. An older panel that ignores the filter answers with users outside the requested ids;
batching then stops for the run. Every service without a usable row, and every chunk whose
call fails, falls back to the per-subscription lookup, so a result is never worse than before.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.services.pasarguard import PasarGuardError

logger = logging.getLogger(__name__)

ID_CHUNK = 100
LIST_TIMEOUT_SECONDS = 30
SINGLE_TIMEOUT_SECONDS = 12
SINGLE_CONCURRENCY = 8
_PANEL_ERRORS = (httpx.HTTPError, PasarGuardError, TimeoutError, ValueError)


class PanelService(Protocol):
    pg_user_id: int | None
    pg_username: str | None
    subscription_token: str | None


@dataclass(slots=True)
class BatchStats:
    """How the infos of one call were read; for logs and tests."""

    list_calls: int = 0
    single_calls: int = 0
    filter_ignored: bool = False


def _rows(data: Any) -> list[dict[str, Any]]:
    users = (data.get("users") or data.get("items") or []) if isinstance(data, dict) else data
    return [row for row in users if isinstance(row, dict)] if isinstance(users, list) else []


def _panel_id(service: PanelService) -> int | None:
    pg_id = service.pg_user_id
    return pg_id if isinstance(pg_id, int) and not isinstance(pg_id, bool) and pg_id > 0 else None


def _same_user(service: PanelService, row: dict[str, Any]) -> bool:
    wanted = (service.pg_username or "").strip().lower()
    got = str(row.get("username") or "").strip().lower()
    return bool(wanted) and wanted == got


async def _list_chunk(pg: Any, ids: list[int], stats: BatchStats) -> dict[int, dict[str, Any]] | None:
    """Rows of one chunk by id: empty after a failed call, None when the panel ignored the filter."""
    stats.list_calls += 1
    try:
        data = await asyncio.wait_for(pg.list_users_by_ids(ids), timeout=LIST_TIMEOUT_SECONDS)
    except _PANEL_ERRORS:
        logger.warning("panel user list by ids failed; reading those services one by one", exc_info=True)
        return {}
    wanted = set(ids)
    rows: dict[int, dict[str, Any]] = {}
    for row in _rows(data):
        row_id = row.get("id")
        if not isinstance(row_id, int) or isinstance(row_id, bool):
            continue
        if row_id not in wanted:
            return None
        rows[row_id] = row
    return rows


async def _listed_rows(pg: Any, services: Sequence[PanelService], stats: BatchStats) -> dict[int, dict[str, Any]]:
    ids = sorted({pg_id for pg_id in map(_panel_id, services) if pg_id is not None})
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), ID_CHUNK):
        rows = await _list_chunk(pg, ids[start : start + ID_CHUNK], stats)
        if rows is None:
            stats.filter_ignored = True
            logger.info("panel ignores the user id filter; reading services one by one")
            break
        found.update(rows)
    missing = len(ids) - len(found)
    if missing and not stats.filter_ignored:
        # Typical when the bot's panel login is not sudo: the list only holds that admin's users.
        logger.info(
            "panel user list returned %s of %s requested services; the rest are read one by one", len(found), len(ids)
        )
    return found


async def _single(pg: Any, token: str | None, gate: asyncio.Semaphore, stats: BatchStats) -> dict[str, Any] | None:
    if not token:
        return None
    async with gate:
        stats.single_calls += 1
        try:
            info = await asyncio.wait_for(pg.subscription_info(token), timeout=SINGLE_TIMEOUT_SECONDS)
        except Exception:  # one unreadable service must not stop the batch; it is retried next run
            logger.debug("subscription lookup failed", exc_info=True)
            return None
    return info if isinstance(info, dict) else None


async def panel_infos(
    pg: Any, services: Sequence[PanelService], *, stats: BatchStats | None = None
) -> list[dict[str, Any] | None]:
    """The live panel row of each service, in order; None where it could not be read."""
    stats = stats if stats is not None else BatchStats()
    listed = await _listed_rows(pg, services, stats)
    gate = asyncio.Semaphore(SINGLE_CONCURRENCY)

    async def one(service: PanelService) -> dict[str, Any] | None:
        pg_id = _panel_id(service)
        row = listed.get(pg_id) if pg_id is not None else None
        if row is not None and _same_user(service, row):
            return row
        return await _single(pg, service.subscription_token, gate, stats)

    return list(await asyncio.gather(*(one(service) for service in services)))
