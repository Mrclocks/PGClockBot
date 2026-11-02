"""Mini App home summary built from locally cached service quota, without panel calls.

The home screen opens on every Mini App launch, so it reads only ``UserService``
columns that ``sync_service_quota_cache`` keeps fresh whenever a live read happens.
Live usage stays on the services list and the service detail screen.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserService
from app.services.formatting import is_on_hold_status, status_label_plain, time_remaining_label
from app.services.service_scope import shop_services_clause

ATTENTION_DAYS = 3
ATTENTION_LIMIT = 3
RECENT_LIMIT = 3
_ENDED_STATUSES = ("expired", "limited")


@dataclass(slots=True)
class ServiceGlance:
    """One service row on the home screen; ``days_left`` is None when unknown or unlimited."""

    id: int
    username: str
    status: str | None
    status_fa: str
    days_left: int | None
    remaining_label: str
    is_cancelled: bool
    cancellation_pending: bool


@dataclass(slots=True)
class HomeSummary:
    service_count: int
    attention: list[ServiceGlance]
    recent: list[ServiceGlance]

    def payload(self) -> dict:
        return {
            "service_count": self.service_count,
            "attention": [asdict(row) for row in self.attention],
            "recent": [asdict(row) for row in self.recent],
        }


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _days_left(service: UserService, now: datetime) -> int | None:
    if service.quota_synced_at is None or service.quota_expire_at is None:
        return None
    seconds = (_as_utc(service.quota_expire_at) - now).total_seconds()
    return max(0, math.ceil(seconds / 86400))


def _glance(service: UserService, now: datetime) -> ServiceGlance:
    status = (service.quota_status or "").strip() or None
    days = _days_left(service, now)
    synced = service.quota_synced_at is not None
    return ServiceGlance(
        id=int(service.id),
        username=service.pg_username or "",
        status=status,
        status_fa=status_label_plain(status) if status else "نامشخص",
        days_left=days,
        remaining_label=time_remaining_label(days_left=days, status=status)
        if synced or is_on_hold_status(status)
        else "—",
        is_cancelled=bool(service.is_cancelled),
        cancellation_pending=bool(service.cancellation_pending),
    )


async def customer_home_summary(
    session: AsyncSession,
    user_id: int,
    *,
    shop_id: int | None = None,
    now: datetime | None = None,
) -> HomeSummary:
    """Service count, services ending soon (or already ended) and the newest services."""
    now = now or datetime.now(timezone.utc)
    owned = (UserService.bot_user_id == int(user_id)) & shop_services_clause(shop_id)
    count = int(
        await session.scalar(select(func.count()).select_from(UserService).where(owned)) or 0
    )
    if not count:
        return HomeSummary(service_count=0, attention=[], recent=[])
    ending = or_(
        UserService.quota_expire_at <= now + timedelta(days=ATTENTION_DAYS),
        func.lower(UserService.quota_status).in_(_ENDED_STATUSES),
    )
    attention_rows = await session.scalars(
        select(UserService)
        .where(
            owned,
            UserService.is_cancelled.is_(False),
            UserService.quota_synced_at.is_not(None),
            ending,
        )
        # Portable "NULLS LAST": SQLite and PostgreSQL disagree on the default.
        .order_by(
            UserService.quota_expire_at.is_(None),
            UserService.quota_expire_at.asc(),
            UserService.id.desc(),
        )
        .limit(ATTENTION_LIMIT)
    )
    recent_rows = await session.scalars(
        select(UserService).where(owned).order_by(UserService.id.desc()).limit(RECENT_LIMIT)
    )
    return HomeSummary(
        service_count=count,
        attention=[_glance(row, now) for row in attention_rows],
        recent=[_glance(row, now) for row in recent_rows],
    )
