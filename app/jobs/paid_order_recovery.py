"""Orchestration: retry or refund paid orders whose delivery failed (see paid_order_recovery)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.bot import create_bot
from app.db.session import SessionLocal
from app.services.paid_order_recovery import due_failure_ids, notify_recovery_result, recover_one
from app.services.pg_link_watchdog import panel_answers

logger = logging.getLogger(__name__)


async def run_paid_order_recovery() -> None:
    """APScheduler entry: one batch; each order in its own session, never raise out of the job."""
    try:
        async with SessionLocal() as session:
            failure_ids = await due_failure_ids(session, now=datetime.now(timezone.utc))
        if not failure_ids:
            return
        panel_up = await panel_answers()
        bot = None
        try:
            for failure_id in failure_ids:
                async with SessionLocal() as session:
                    try:
                        result = await recover_one(
                            session, failure_id, now=datetime.now(timezone.utc), panel_up=panel_up
                        )
                        if result is None:
                            continue
                        bot = bot or create_bot()
                        await notify_recovery_result(session, bot, result)
                    except Exception:
                        # One bad row must not stop the batch; its lease expires and it is retried.
                        logger.exception("paid order recovery failed failure=%s", failure_id)
        finally:
            if bot is not None:
                await bot.session.close()
    except Exception:
        logger.exception("paid order recovery job failed")
