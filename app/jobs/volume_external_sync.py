"""Periodic recount of panel-only users' volume for every gated shop."""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.services.volume_allocation_external import run_external_allocation_sync

logger = logging.getLogger(__name__)


async def run_volume_external_sync() -> None:
    """Scheduler entry; per-shop failures are logged in the service and keep the old totals."""
    try:
        results = await run_external_allocation_sync(SessionLocal)
    except Exception:
        logger.exception("volume external sync batch failed")
        return
    failed = [r.shop_id for r in results if r.error is not None]
    if failed:
        logger.warning("volume external sync: %s of %s shops kept their last count: %s", len(failed), len(results), failed[:20])
