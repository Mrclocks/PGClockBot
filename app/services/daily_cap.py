"""Per-shop daily caps for manual changes, claimed atomically on a counter row."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShopPointsAdjustDay, ShopWalletAdjustDay

DayCounter = type[ShopWalletAdjustDay] | type[ShopPointsAdjustDay]


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def claim_daily_cap(session: AsyncSession, counter: DayCounter, *, shop_id: int, amount: int, cap: int) -> bool:
    """Add ``amount`` to today's total for the shop when it stays within ``cap``.

    The conditional UPDATE is the claim: two requests at the same moment cannot both pass,
    and it is undone with the caller's transaction if the change itself fails.
    """
    day = utc_day()
    try:
        async with session.begin_nested():
            session.add(counter(shop_id=shop_id, day=day, used=0))
            await session.flush()
    except IntegrityError:
        pass
    claimed = await session.execute(
        update(counter)
        .where(counter.shop_id == shop_id, counter.day == day, counter.used + amount <= cap)
        .values(used=counter.used + amount)
        .execution_options(synchronize_session=False)
    )
    return claimed.rowcount == 1
