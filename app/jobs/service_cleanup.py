"""Scheduler job: warn customers about, then delete, their expired or volume-finished services."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal
from app.services.service_cleanup import KIND_TIME, KIND_VOLUME, CleanupPolicy, enabled_scopes
from app.services.service_cleanup_delete import DELETE_BATCH, DeleteResult, delete_due_services
from app.services.service_cleanup_notices import scan_notices

logger = logging.getLogger(__name__)

MAX_BATCHES_PER_SCOPE = 5  # a larger backlog of warnings or deletions is finished by the next runs
STEP_NOTICES = "notices"
STEP_DELETE = "delete"
# Where each step of each (scope, rule) stopped: the next run continues there, so services that stay due but
# cannot be handled now (an unreadable panel, no Telegram chat) never keep the first batches full forever.
# Kept in memory only; after a restart a step simply starts again from the lowest id.
_resume_after: dict[tuple[str, int | None, str], int] = {}


async def _notice_batches(session: AsyncSession, shop_id: int | None, policy: CleanupPolicy, kind: str) -> None:
    """Queue warnings batch after batch, continuing from where the previous run stopped."""
    key = (STEP_NOTICES, shop_id, kind)
    for _ in range(MAX_BATCHES_PER_SCOPE):
        result = await scan_notices(session, shop_id=shop_id, policy=policy, kind=kind, after_id=_resume_after.get(key, 0))
        if result.next_after_id is None:
            _resume_after.pop(key, None)
            return
        _resume_after[key] = result.next_after_id


async def _delete_batches(session: AsyncSession, shop_id: int | None, policy: CleanupPolicy, kind: str) -> DeleteResult:
    """Delete in id order, batch after batch, continuing from where the previous run stopped."""
    key = (STEP_DELETE, shop_id, kind)
    deleted = skipped = failed = 0
    for _ in range(MAX_BATCHES_PER_SCOPE):
        outcome = await delete_due_services(
            session, shop_id=shop_id, policy=policy, kind=kind, limit=DELETE_BATCH, after_id=_resume_after.get(key, 0)
        )
        deleted, skipped, failed = deleted + outcome.deleted, skipped + outcome.skipped, failed + outcome.failed
        if outcome.next_after_id is None:
            _resume_after.pop(key, None)
            break
        _resume_after[key] = outcome.next_after_id
    return DeleteResult(deleted=deleted, skipped=skipped, failed=failed)


async def run_service_cleanup() -> None:
    """One pass over every scope with a rule on; a failing scope is logged and the others still run."""
    try:
        async with SessionLocal() as session:
            scopes = await enabled_scopes(session)
            for shop_id, policy in scopes:
                for kind in (KIND_TIME, KIND_VOLUME):
                    try:
                        await _notice_batches(session, shop_id, policy, kind)
                        outcome = await _delete_batches(session, shop_id, policy, kind)
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
