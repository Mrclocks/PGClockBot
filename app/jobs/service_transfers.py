"""Scheduler job: rotate the subscription link of transferred services whose first try failed."""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.services.service_transfer import retry_link_rotations

logger = logging.getLogger(__name__)


async def run_service_transfers() -> None:
    """One pass; a failed pass is logged and retried on the next run."""
    try:
        async with SessionLocal() as session:
            await retry_link_rotations(session)
    except Exception:  # noqa: BLE001 - background job of last resort; the rows stay pending
        logger.exception("service transfer link rotation pass failed")
