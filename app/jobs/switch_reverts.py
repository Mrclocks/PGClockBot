"""Orchestration: put timed quick switches back when their time comes (see ``switch_schedule``)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal
from app.services.switch_schedule import run_due_reverts

logger = logging.getLogger(__name__)


async def run_switch_reverts() -> None:
    """APScheduler entry: one batch, one indexed query when nothing is due; never raises."""
    try:
        applied = await run_due_reverts(SessionLocal, now=datetime.now(timezone.utc))
    except SQLAlchemyError:
        logger.warning("switch revert batch failed", exc_info=True)
        return
    if applied:
        logger.info("switch reverts applied=%s", applied)
