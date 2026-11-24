"""«همگام‌سازی دوباره با پنل»: read every live service from PasarGuard again and store its expiry and volume.

The users list and many screens read each service's expiry and volume from the copy kept on the service row,
which the panel refreshes whenever it reads a service anyway. After changes made straight on PasarGuard that
copy can be old. This run reads every service with a panel user, in pages of ``RESYNC_BATCH`` (keyset on id,
so the table is never loaded whole), with the batched panel read (one list call per hundred users), and
writes only the cached expiry and volume. It runs in the background, one run at a time; a page whose panel
call fails is skipped and the run goes on.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import UserService
from app.db.session import SessionLocal
from app.services.bot_user_admin import sync_service_quota_cache
from app.services.db_safe import rollback_quiet
from app.services.panel_user_batch import panel_infos
from app.services.pasarguard import get_pg
from app.services.service_live_info import validate_service_info

logger = logging.getLogger(__name__)

RESYNC_BATCH = 100


@dataclass(slots=True)
class ResyncRun:
    started_at: datetime
    read: int = 0
    synced: int = 0
    unread: int = 0
    failed_pages: int = 0
    finished_at: datetime | None = None


_task: asyncio.Task[None] | None = None
_last_run: ResyncRun | None = None


def resync_running() -> bool:
    return _task is not None and not _task.done()


def last_resync() -> ResyncRun | None:
    return _last_run


def _store(services: list[UserService], infos: list[dict | None], run: ResyncRun) -> None:
    for service, info in zip(services, infos, strict=True):
        checked = validate_service_info(info) if info else {}
        if checked.get("status") and "expire" in checked:
            sync_service_quota_cache(service, info=checked)
            run.synced += 1
        else:
            run.unread += 1


async def _resync_page(pg: object, after_id: int, run: ResyncRun) -> int | None:
    """Sync one page after ``after_id``; returns the last id, or None when there are no more services."""
    async with SessionLocal() as session:
        services = list(await session.scalars(
            select(UserService)
            .where(UserService.id > after_id, UserService.pg_user_id.is_not(None), UserService.is_cancelled.is_(False))
            .order_by(UserService.id).limit(RESYNC_BATCH)
        ))
        if not services:
            return None
        # No transaction stays open while the panel answers; the loaded rows stay usable.
        await session.commit()
        try:
            infos = await panel_infos(pg, services)
            _store(services, infos, run)
            await session.commit()
        except Exception:  # noqa: BLE001 - one page must not stop the run; its rows keep their old copy
            logger.warning("service resync page failed after_id=%s", after_id, exc_info=True)
            await rollback_quiet(session)
            run.failed_pages += 1
        run.read += len(services)
        return int(services[-1].id)


async def _resync_all(run: ResyncRun) -> None:
    try:
        pg = get_pg()
        after_id: int | None = 0
        while after_id is not None:
            after_id = await _resync_page(pg, after_id, run)
    except Exception:  # noqa: BLE001 - background task of last resort; the run is shown as finished
        logger.exception("service resync stopped")
    run.finished_at = datetime.now(timezone.utc)
    logger.info("service resync done read=%s synced=%s unread=%s failed_pages=%s",
                run.read, run.synced, run.unread, run.failed_pages)


def start_resync() -> None:
    """Start a run in the background; ValueError when one is already going."""
    global _task, _last_run
    if resync_running():
        raise ValueError("همگام‌سازی با پنل همین حالا در جریان است")
    _last_run = ResyncRun(started_at=datetime.now(timezone.utc))
    _task = asyncio.create_task(_resync_all(_last_run), name="service-resync")
