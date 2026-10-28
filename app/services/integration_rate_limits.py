"""Atomic UTC-minute admission shared by every worker and credential."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Table, update
from sqlalchemy.dialects.postgresql import Insert as PGInsert, insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert, insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_security_models import IntegrationRateCounter
from app.services.integration_policy import IntegrationError

LIMITS = {"read": 120, "write": 20, "private": 10, "preauth": 60, "audit": 1000}
COUNTER_RETENTION_MINUTES = 48 * 60
WRITE_SCOPES = frozenset({"customers:write", "services:provision", "services:renew", "services:addons", "telegram:link"})


class RequestBudget(str, Enum):
    READ = "read"
    WRITE = "write"
    PRIVATE = "private"


class IntegrationRateLimited(IntegrationError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate_limited")
        self.retry_after = max(1, min(60, retry_after))


@dataclass(frozen=True, slots=True)
class RateWindow:
    minute: int
    retry_after: int


def rate_window(now: datetime | None = None) -> RateWindow:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise IntegrationError("invalid_request")
    seconds = int(now.timestamp())
    return RateWindow(seconds // 60, 60 - seconds % 60)


def conflict_insert(session: AsyncSession, table: Table) -> PGInsert | SQLiteInsert:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite_insert(table)
    if dialect == "postgresql":
        return pg_insert(table)
    raise IntegrationError("security_unavailable")


async def take_slot(session: AsyncSession, subject: str, budget: str, window: RateWindow) -> bool:
    """The conditional upsert also serializes first-use races on independent connections."""
    table = IntegrationRateCounter.__table__
    statement = conflict_insert(session, table).values(
        subject=subject, budget=budget, window=window.minute, used=1,
    ).on_conflict_do_update(
        index_elements=[table.c.subject, table.c.budget, table.c.window],
        set_={"used": table.c.used + 1}, where=table.c.used < LIMITS[budget],
    ).returning(table.c.used)
    return (await session.scalar(statement)) is not None


async def reserve_preauth(session: AsyncSession, ip_digest: str, window: RateWindow) -> None:
    allowed = await take_slot(session, ip_digest, "preauth", window)
    await session.commit()
    if not allowed:
        raise IntegrationRateLimited(window.retry_after)


async def release_preauth(session: AsyncSession, ip_digest: str, window: RateWindow) -> None:
    await session.execute(update(IntegrationRateCounter).where(
        IntegrationRateCounter.subject == ip_digest, IntegrationRateCounter.budget == "preauth",
        IntegrationRateCounter.window == window.minute, IntegrationRateCounter.used > 0,
    ).values(used=IntegrationRateCounter.used - 1))


async def charge_integration(
    session: AsyncSession, integration_id: str, budget: RequestBudget, *, charged: set[str],
) -> None:
    window = rate_window()
    budgets = ("read", "private") if budget == RequestBudget.PRIVATE else (budget.value,)
    for name in budgets:
        if name not in charged and not await take_slot(session, integration_id, name, window):
            # An exhausted private limit must not partially consume the read limit.
            await session.rollback()
            raise IntegrationRateLimited(window.retry_after)
    await session.commit()
    charged.update(budgets)
