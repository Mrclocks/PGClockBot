"""Orchestration: run the PasarGuard link watchdog on a fixed interval."""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.services.pg_link_watchdog import run_watchdog_tick

logger = logging.getLogger(__name__)


async def run_pg_link_watchdog() -> None:
    """APScheduler entry: one tick, never raise out of the job."""
    try:
        async with SessionLocal() as session:
            result = await run_watchdog_tick(session)
        if result.rebuilt:
            logger.warning("PasarGuard link watchdog rebuilt HTTP client kind=%s", result.failure_kind)
    except Exception:
        logger.warning("PasarGuard link watchdog job failed", exc_info=True)
