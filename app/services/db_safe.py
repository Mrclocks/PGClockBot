"""Shared DB session recovery helpers — never leave a poisoned session mid-request."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def rollback_quiet(session: AsyncSession | None) -> None:
    """Roll back after a caught DB error so later queries on the same session can run.

    SQLAlchemy leaves the session in ``PendingRollbackError`` until rollback;
    swallowing an exception without this commonly 500s the rest of the request
    (e.g. sidebar unread fails → dashboard aggregates fail).
    """
    if session is None:
        return
    try:
        await session.rollback()
    except Exception:
        logger.debug("session rollback failed", exc_info=True)


async def rollback_and_reload(session: AsyncSession, *instances: object) -> None:
    """rollback_quiet, then reload what the rollback expired so the caller can keep reading it.

    A rollback expires the objects loaded in that transaction. An async session cannot load an
    expired attribute on access (MissingGreenlet), so code that goes on using an object after a
    caught error, such as a delivered order after a failed reward step, refreshes it here first.
    """
    await rollback_quiet(session)
    for instance in instances:
        await session.refresh(instance)


async def recover_session(session: AsyncSession | None) -> None:
    """Heal a session that is stuck needing rollback before the next query.

    Safe to call on a healthy session (no-op). Use after ``require_staff`` and
    before page sections that catch-and-continue past DB errors.
    """
    if session is None:
        return
    try:
        from sqlalchemy.exc import PendingRollbackError

        try:
            await session.connection()
            return
        except PendingRollbackError:
            logger.warning("recovering session after PendingRollbackError")
            await rollback_quiet(session)
            return
    except Exception:
        # Fall through to transactional-state probe
        logger.debug("session connection probe failed; checking its transaction state", exc_info=True)
    try:
        sync = getattr(session, "sync_session", None)
        if sync is None:
            return
        tx = sync.get_transaction()
        if tx is not None and getattr(tx, "_rollback_exception", None) is not None:
            logger.warning("recovering session after rollback_exception marker")
            await rollback_quiet(session)
    except Exception:
        logger.debug("recover_session probe failed", exc_info=True)
