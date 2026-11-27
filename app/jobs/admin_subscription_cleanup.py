"""Daily removal of subscription rows whose PasarGuard admin is gone."""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.services.admin_subscription_cleanup import prune_leftover_subscriptions
from app.services.pasarguard import get_pg

logger = logging.getLogger(__name__)


async def run_admin_subscription_cleanup() -> None:
    """Scheduler entry; a row is kept whenever the panel cannot confirm its admin is missing."""
    try:
        removed = await prune_leftover_subscriptions(SessionLocal, get_pg())
    except Exception:
        logger.exception("admin subscription cleanup failed")
        return
    if removed:
        logger.info("admin subscription cleanup removed %s rows: %s", len(removed), removed[:20])
