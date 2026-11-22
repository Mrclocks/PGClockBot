"""Monthly budget for automatic discount rules, claimed atomically per order.

A shop sets how much automatic-rule discount it gives per calendar month (0 = no limit). A
rule is applied only when its reduction still fits: ``claim_budget`` adds it to the month's
``used`` with one conditional UPDATE, so concurrent checkouts never overspend. The use is
counted with or without a limit. Cancelling an unpaid order gives the amount back to the month
the reduction was taken in.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscountBudget
from app.services.users import get_setting, set_setting

logger = logging.getLogger(__name__)

BUDGET_SETTING = "discount_rules_monthly_budget"
MAX_BUDGET = 2_000_000_000


def _month(at: datetime) -> str:
    return at.strftime("%Y-%m")


async def monthly_budget(session: AsyncSession, shop_id: int | None) -> int:
    raw = str(await get_setting(session, BUDGET_SETTING, "0", reseller_id=shop_id) or "0")
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 <= value <= MAX_BUDGET else 0


async def save_monthly_budget(session: AsyncSession, shop_id: int | None, amount: int) -> None:
    if type(amount) is not int or not 0 <= amount <= MAX_BUDGET:
        raise ValueError("«بودجهٔ ماهانه» خارج از محدودهٔ مجاز است")
    await set_setting(session, BUDGET_SETTING, str(amount), reseller_id=shop_id)
    await session.commit()


async def budget_used(session: AsyncSession, shop_id: int | None, *, at: datetime) -> int:
    used = await session.scalar(select(DiscountBudget.used).where(
        DiscountBudget.shop_key == int(shop_id or 0), DiscountBudget.month == _month(at),
    ))
    return int(used or 0)


async def claim_budget(session: AsyncSession, shop_key: int, amount: int, *, budget: int, at: datetime) -> bool:
    """Add ``amount`` to the month's use if ``budget`` allows (0 = no limit); caller commits.

    The use is counted even without a limit, so the page shows the month's spend and a limit set
    mid-month starts from the real figure.
    """
    month = _month(at)
    try:
        async with session.begin_nested():
            session.add(DiscountBudget(shop_key=shop_key, month=month, used=0))
            await session.flush()
    except IntegrityError:
        logger.debug("discount budget row exists shop=%s month=%s", shop_key, month)
    within = [] if budget == 0 else [DiscountBudget.used + int(amount) <= budget]
    claimed = await session.execute(
        update(DiscountBudget)
        .where(DiscountBudget.shop_key == shop_key, DiscountBudget.month == month, *within)
        .values(used=DiscountBudget.used + int(amount))
        .execution_options(synchronize_session=False)
    )
    return claimed.rowcount == 1


async def give_back_budget(session: AsyncSession, shop_key: int, amount: int, *, at: datetime) -> None:
    """Return a released reduction to the month it was taken in; never below zero."""
    await session.execute(
        update(DiscountBudget)
        .where(DiscountBudget.shop_key == shop_key, DiscountBudget.month == _month(at),
               DiscountBudget.used >= int(amount))
        .values(used=DiscountBudget.used - int(amount))
        .execution_options(synchronize_session=False)
    )
