"""Scheduler job: find out what the panel did with customer link changes whose answer was lost."""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.services.service_link_reset import process_open_link_resets

logger = logging.getLogger(__name__)


async def run_service_link_resets() -> None:
    """One pass; a failed pass is logged and retried on the next run."""
    try:
        async with SessionLocal() as session:
            await process_open_link_resets(session)
    except Exception:  # noqa: BLE001 - background job of last resort; the changes stay open
        logger.exception("service link reset pass failed")
