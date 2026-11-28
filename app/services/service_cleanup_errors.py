"""The failure the expired-service cleanup met on its last try of a service, stored on the row.

The worker stores a code when it could not handle a service and clears it after its next good
read, so the cleanup page can say why a service past its deadline is still there.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserService

ERROR_PANEL_UNREADABLE = "panel_unreadable"
ERROR_DELETE_FAILED = "delete_failed"


async def record_cleanup_error(session: AsyncSession, service_ids: list[int], code: str, *, now: datetime) -> None:
    """Store ``code`` on these services; the caller commits."""
    if service_ids:
        await session.execute(
            update(UserService).where(UserService.id.in_(service_ids)).values(cleanup_error=code, cleanup_error_at=now),
            execution_options={"synchronize_session": False},
        )


async def clear_cleanup_error(session: AsyncSession, service_ids: list[int]) -> None:
    """Forget an earlier failure of services the worker could handle again; the caller commits."""
    if service_ids:
        await session.execute(
            update(UserService)
            .where(UserService.id.in_(service_ids), UserService.cleanup_error.is_not(None))
            .values(cleanup_error=None, cleanup_error_at=None),
            execution_options={"synchronize_session": False},
        )
