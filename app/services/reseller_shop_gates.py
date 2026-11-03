"""Shared gates for a shop owner's own subscription purchases (renew, packs, unit extras).

Used by bot, web and Mini App through the same service entry points so one surface cannot
bypass a check another enforces. Fail closed on inactive shops and closed/expired clocks.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerProfile
from app.services.pg_admin_subscription import STATUS_ACTIVE, get_or_create_subscription

SHOP_INACTIVE = "فروشگاه نمایندگی غیرفعال است"
SUB_NEEDS_ACTIVE = "فقط اشتراک فعال می‌تواند بسته اضافه بخرد — ابتدا تمدید کنید"
SUB_EXPIRED = "اشتراک منقضی است — ابتدا تمدید کنید"


def assert_shop_active(profile: ResellerProfile) -> None:
    """Refuse capacity and renew mutations when the shop profile is soft-deactivated."""
    if not bool(getattr(profile, "is_active", False)):
        raise ValueError(SHOP_INACTIVE)


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


async def assert_subscription_open(session: AsyncSession, uname: str) -> None:
    """Only an active, unexpired subscription can take more capacity."""
    sub = await get_or_create_subscription(session, uname)
    if sub.access_status != STATUS_ACTIVE:
        raise ValueError(SUB_NEEDS_ACTIVE)
    expires = _aware(sub.expires_at)
    if expires is not None and expires <= datetime.now(timezone.utc):
        raise ValueError(SUB_EXPIRED)
