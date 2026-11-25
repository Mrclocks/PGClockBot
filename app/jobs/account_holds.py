"""Carry out account holds that are still turning a customer or shop off, or back on."""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.services.account_hold_runner import run_unfinished_holds
from app.services.pasarguard import get_pg

logger = logging.getLogger(__name__)


async def run_account_holds() -> None:
    """Scheduler entry; each hold keeps its saved state when the panel cannot be reached."""
    try:
        finished = await run_unfinished_holds(SessionLocal, get_pg())
    except Exception:
        logger.exception("account holds pass failed")
        return
    if finished:
        logger.info("account holds finished: %s", finished)
