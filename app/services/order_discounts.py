"""The order discount ledger: one line per reduction source on an order.

Each place that lowers an order's price records a line here (``record_order_discount``), and
each place that hands an unpaid order's reductions back releases them
(``release_order_discounts``). Reports and per-customer limits count the live lines of a
shop instead of parsing ``Order.discount_code``.

A line is recorded once per (order, source): a repeat is a no-op, so a retried checkout never
counts a reduction twice. Lines carry ``shop_key`` (0 for the platform shop) copied from the
order, never from a request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChargeCode, Order, OrderDiscount

SOURCE_CODE = "code"
SOURCE_GIFT = "gift"
SOURCE_LOYALTY = "loyalty"
SOURCE_RANDOM = "random"
SOURCE_WHOLESALE = "wholesale"
SOURCE_RULE = "rule"
SOURCES = (SOURCE_CODE, SOURCE_GIFT, SOURCE_LOYALTY, SOURCE_RANDOM, SOURCE_WHOLESALE, SOURCE_RULE)
SOURCE_REF_LENGTH = 64
LOYALTY_CODE_PREFIX = "LOY"
WHOLESALE_CODE_PREFIX = "wholesale:"


def order_shop_key(order: Order) -> int:
    return int(order.reseller_id or 0)


async def code_source(session: AsyncSession, code: str) -> str:
    """The ledger source of a reserved ``Order.discount_code``."""
    if code.startswith(WHOLESALE_CODE_PREFIX):
        return SOURCE_WHOLESALE
    normalized = code.strip().upper()
    if normalized.startswith(LOYALTY_CODE_PREFIX):
        return SOURCE_LOYALTY
    gift = await session.scalar(select(ChargeCode.id).where(ChargeCode.code == normalized))
    return SOURCE_GIFT if gift is not None else SOURCE_CODE


async def record_order_discount(
    session: AsyncSession, order: Order, *, source: str, amount: int, source_ref: str | None = None,
) -> bool:
    """Add the line for ``source`` on ``order`` inside the caller's transaction; False if it exists."""
    if source not in SOURCES:
        raise ValueError(f"unknown discount source: {source}")
    if type(amount) is not int or amount <= 0:
        return False
    ref = None if source_ref is None else str(source_ref)[:SOURCE_REF_LENGTH]
    try:
        async with session.begin_nested():
            session.add(OrderDiscount(
                order_id=int(order.id), shop_key=order_shop_key(order), user_id=int(order.user_id),
                source=source, source_ref=ref, amount=amount,
            ))
            await session.flush()
    except IntegrityError:
        return False
    return True


async def record_code_discount(session: AsyncSession, order: Order, *, amount: int) -> bool:
    """Record the reduction of the code now reserved on ``order``."""
    code = order.discount_code
    if not code:
        return False
    source = await code_source(session, code)
    return await record_order_discount(session, order, source=source, amount=amount, source_ref=code)


async def release_order_discounts(session: AsyncSession, order_id: int, *, sources: tuple[str, ...] = SOURCES) -> int:
    """Mark the order's live lines released (unpaid order cancelled); returns how many changed."""
    result = await session.execute(
        update(OrderDiscount)
        .where(
            OrderDiscount.order_id == int(order_id),
            OrderDiscount.source.in_(sources),
            OrderDiscount.released_at.is_(None),
        )
        .values(released_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def live_order_discounts(session: AsyncSession, order_id: int) -> list[OrderDiscount]:
    return list((await session.scalars(
        select(OrderDiscount)
        .where(OrderDiscount.order_id == int(order_id), OrderDiscount.released_at.is_(None))
        .order_by(OrderDiscount.id)
        .limit(len(SOURCES))
    )).all())

