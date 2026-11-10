"""Scheduler job: warn customers about, then delete, their expired or volume-finished services."""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.services.service_cleanup import KIND_TIME, KIND_VOLUME, enabled_scopes
from app.services.service_cleanup_delete import DELETE_BATCH, delete_due_services
from app.services.service_cleanup_notices import SCAN_BATCH, scan_notices

logger = logging.getLogger(__name__)

MAX_BATCHES_PER_SCOPE = 5  # a larger backlog of warnings is finished by the next runs


async def run_service_cleanup() -> None:
    """One pass over every scope with a rule on; a failing scope is logged and the others still run."""
    try:
        async with SessionLocal() as session:
            scopes = await enabled_scopes(session)
            for shop_id, policy in scopes:
                for kind in (KIND_TIME, KIND_VOLUME):
                    try:
                        for _ in range(MAX_BATCHES_PER_SCOPE):
                            result = await scan_notices(session, shop_id=shop_id, policy=policy, kind=kind)
                            if result.queued + result.reset < SCAN_BATCH:
                                break
                        outcome = await delete_due_services(session, shop_id=shop_id, policy=policy, kind=kind, limit=DELETE_BATCH)
                        if outcome.deleted or outcome.failed:
                            logger.info(
                                "service cleanup shop=%s kind=%s deleted=%s failed=%s skipped=%s",
                                shop_id, kind, outcome.deleted, outcome.failed, outcome.skipped,
                            )
                    except Exception:  # noqa: BLE001 - one scope must not stop the others
                        logger.exception("service cleanup notices failed shop=%s kind=%s", shop_id, kind)
                        await session.rollback()
    except Exception:  # noqa: BLE001 - a failed pass is logged and retried on the next run
        logger.exception("service cleanup pass failed")
