"""Shared gates for a shop owner's own subscription purchases (renew, packs, unit extras).

Used by bot, web and Mini App through the same service entry points so one surface cannot
bypass a check another enforces.

``ResellerProfile.is_active`` is flipped off both by an operator soft-deactivate and by the
subscription expiry tick. Capacity buys therefore key off the subscription clock
(``access_status`` / ``expires_at``). Renew is allowed when the clock is expired even if the
profile flag is off; only a soft-deactivate while the subscription is still open is refused.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerProfile
from app.services.pg_admin_subscription import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    get_or_create_subscription,
)

SHOP_INACTIVE = "فروشگاه نمایندگی غیرفعال است"
SUB_NEEDS_ACTIVE = "فقط اشتراک فعال می‌تواند بسته اضافه بخرد — ابتدا تمدید کنید"
SUB_EXPIRED = "اشتراک منقضی است — ابتدا تمدید کنید"


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _pg_username(profile: ResellerProfile) -> str:
    return (getattr(profile, "pg_admin_username", None) or "").strip()


def _clock_is_due(sub) -> bool:
    if getattr(sub, "access_status", None) == STATUS_EXPIRED:
        return True
    expires = _aware(getattr(sub, "expires_at", None))
    return expires is not None and expires <= datetime.now(timezone.utc)


async def assert_subscription_open(session: AsyncSession, uname: str) -> None:
    """Only an active, unexpired subscription can take more capacity."""
    sub = await get_or_create_subscription(session, uname)
    if sub.access_status != STATUS_ACTIVE:
        raise ValueError(SUB_NEEDS_ACTIVE)
    expires = _aware(sub.expires_at)
    if expires is not None and expires <= datetime.now(timezone.utc):
        raise ValueError(SUB_EXPIRED)


async def assert_shop_allows_capacity(session: AsyncSession, profile: ResellerProfile) -> None:
    """Refuse capacity growth when the subscription is closed or the shop was soft-deactivated.

    An expiry tick also sets ``profile.is_active = False``; that case is refused by the
    subscription clock, not by the soft-deactivate message, so the owner is told to renew.
    """
    uname = _pg_username(profile)
    if not uname:
        raise ValueError("ادمین پاسارگارد برای این نماینده تنظیم نشده است")
    await assert_subscription_open(session, uname)
    if bool(getattr(profile, "is_active", False)):
        return
    # Open clock + inactive profile ⇒ operator soft-deactivate (not expiry).
    raise ValueError(SHOP_INACTIVE)


async def assert_shop_allows_renew(session: AsyncSession, profile: ResellerProfile) -> None:
    """Refuse renew only for operator soft-deactivate while the subscription is still open.

    Expired (or due) shops must renew even though the expiry tick flipped ``is_active`` off.
    """
    if bool(getattr(profile, "is_active", False)):
        return
    uname = _pg_username(profile)
    if not uname:
        raise ValueError(SHOP_INACTIVE)
    sub = await get_or_create_subscription(session, uname)
    if _clock_is_due(sub):
        return
    raise ValueError(SHOP_INACTIVE)


# Back-compat alias: capacity paths that already open-checked should use assert_shop_allows_capacity.
async def assert_shop_active(session: AsyncSession, profile: ResellerProfile) -> None:
    await assert_shop_allows_capacity(session, profile)
