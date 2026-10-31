"""Let integration callers include a domain write in their own transaction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


async def finish_integration_write(session: AsyncSession, *, commit: bool) -> None:
    if commit:
        await session.commit()
    else:
        await session.flush()
