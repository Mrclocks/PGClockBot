"""Staff controls over single services in the cleanup queue: keep one forever, or give it more days.

Both change only the acting shop's own services (the scope is part of the UPDATE), take effect before the
next run, and never touch the panel. A hold also lowers the warning stage to the notice, so the customer
is warned again before the new deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, UserService
from app.services.service_cleanup import (
    MAX_DAYS,
    MIN_DAYS,
    STAGE_NOTICE,
    USERNAME_PREVIEW_MAX,
    CleanupPolicy,
    CleanupPolicyError,
    KIND_TIME,
    KIND_VOLUME,
    deadline_for,
    local_date,
    shop_services_clause,
)

EXEMPT_LIST_LIMIT = 50
NOT_FOUND_TEXT = "سرویس پیدا نشد"


@dataclass(frozen=True, slots=True)
class ExemptRow:
    service_id: int
    username: str
    customer: str


def parse_hold_days(raw: object) -> int:
    """Days to add to a deadline: a whole number from MIN_DAYS to MAX_DAYS; raises ``CleanupPolicyError``."""
    text = str(raw or "").strip()
    if not text.isdigit() or len(text) > 4 or not MIN_DAYS <= int(text) <= MAX_DAYS:
        raise CleanupPolicyError(f"تعداد روز باید عددی بین {MIN_DAYS} و {MAX_DAYS} باشد")
    return int(text)


async def set_exempt(session: AsyncSession, *, shop_id: int | None, service_id: int, exempt: bool) -> None:
    """Keep (or stop keeping) one service out of the cleanup; raises ``ValueError`` when it is not this scope's."""
    result = await session.execute(
        update(UserService)
        .where(UserService.id == int(service_id), shop_services_clause(shop_id))
        .values(cleanup_exempt=exempt),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ValueError(NOT_FOUND_TEXT)
    await session.commit()


async def extend_deadline(
    session: AsyncSession, *, shop_id: int | None, service_id: int, days: int, policy: CleanupPolicy,
    now: datetime | None = None,
) -> str:
    """Give one service ``days`` more days than it has now; returns the new deadline date.

    The new hold is the current deadline (or now, if that is earlier) plus ``days``. The warning stage drops
    to the notice so the customer is warned again before the new deadline.
    """
    now = now or datetime.now(timezone.utc)
    service = await session.scalar(
        select(UserService).where(UserService.id == int(service_id), shop_services_clause(shop_id))
    )
    if service is None:
        raise ValueError(NOT_FOUND_TEXT)
    current = max(
        (d for d in (deadline_for(service, policy, KIND_TIME), deadline_for(service, policy, KIND_VOLUME)) if d is not None),
        default=now,
    )
    hold = max(current, now) + timedelta(days=days)
    result = await session.execute(
        update(UserService)
        .where(UserService.id == int(service_id), shop_services_clause(shop_id))
        .values(
            cleanup_hold_until=hold,
            cleanup_warn_stage=_lowered(UserService.cleanup_warn_stage),
            cleanup_volume_warn_stage=_lowered(UserService.cleanup_volume_warn_stage),
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ValueError(NOT_FOUND_TEXT)
    await session.commit()
    return local_date(hold)


def _lowered(column):
    """SQL: ``min(column, STAGE_NOTICE)`` without a dialect-specific function."""
    from sqlalchemy import case

    return case((column > STAGE_NOTICE, STAGE_NOTICE), else_=column)


async def list_exempt(session: AsyncSession, *, shop_id: int | None) -> list[ExemptRow]:
    rows = (
        await session.execute(
            select(UserService.id, UserService.pg_username, BotUser.full_name, BotUser.username)
            .join(BotUser, BotUser.id == UserService.bot_user_id)
            .where(shop_services_clause(shop_id), UserService.cleanup_exempt.is_(True))
            .order_by(UserService.id)
            .limit(EXEMPT_LIST_LIMIT)
        )
    ).all()
    return [
        ExemptRow(int(i), str(name or "")[:USERNAME_PREVIEW_MAX], str(full or (f"@{handle}" if handle else "—"))[:USERNAME_PREVIEW_MAX])
        for i, name, full, handle in rows
    ]
