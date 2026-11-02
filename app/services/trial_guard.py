"""Waiting period a brand-new account must pass before it can take a free trial.

A bot that registers an account and asks for the trial in the same second is the usual
abuse pattern; a short wait costs a real customer nothing and every extra account time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser

MIN_ACCOUNT_AGE_KEY = "trial_min_account_minutes"
MAX_WAIT_MINUTES = 7 * 24 * 60
SECONDS_PER_MINUTE = 60


def parse_min_account_age(raw: object) -> int:
    """The configured wait in minutes: 0 (off) for anything that is not a sane whole number."""
    text = str(raw or "").strip()
    if not text.isascii() or not text.isdigit():
        return 0
    return min(int(text), MAX_WAIT_MINUTES)


def trial_wait_minutes(created_at: datetime | None, *, min_age_minutes: int, now: datetime | None = None) -> int:
    """Whole minutes still to wait; 0 when the account is old enough or the gate is off."""
    if min_age_minutes <= 0 or created_at is None:
        return 0
    now = now or datetime.now(timezone.utc)
    # SQLite returns naive values for timezone-aware columns.
    created = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    remaining = min_age_minutes * SECONDS_PER_MINUTE - (now - created).total_seconds()
    return max(0, math.ceil(remaining / SECONDS_PER_MINUTE))


async def assert_trial_account_age(session: AsyncSession, user_id: int, *, settings: Mapping[str, str]) -> None:
    """Raise ValueError while the account is younger than the shop's waiting period."""
    min_age = parse_min_account_age(settings.get(MIN_ACCOUNT_AGE_KEY))
    if not min_age:
        return
    user = await session.get(BotUser, int(user_id))
    wait = trial_wait_minutes(user.created_at if user else None, min_age_minutes=min_age)
    if wait:
        raise ValueError(f"برای دریافت تست باید {wait} دقیقهٔ دیگر از ورود شما به ربات بگذرد؛ کمی بعد دوباره تلاش کنید")
