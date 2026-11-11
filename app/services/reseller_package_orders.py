"""Package renew/upgrade paid like a plan: an order any enabled method can settle, applied once.

The note freezes the target (``res_pkg:{plan_id}:{generation}``) and ``order.amount`` freezes the
invoice total. Orders belong to the platform (no shop): money goes to the platform's payment
methods. Applying claims the paid order, renews without a second wallet debit, and updates the
profile plan when the target differs. A stale generation (another renew already happened) leaves
the order paid for staff; a panel refusal undoes the subscription change and returns the order
to paid.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, ResellerProfile
from app.services.db_safe import rollback_quiet
from app.services.reseller_package import (
    RELATION_CURRENT,
    STALE_PAGE,
    package_overview,
    renew_package,
)

logger = logging.getLogger(__name__)

NOTE_PREFIX = "res_pkg:"
_NOTE_RE = re.compile(r"^res_pkg:(\d{1,10}):(\d{1,10})$")

INVALID_ORDER = "سفارش تمدید بسته نامعتبر است"
NOT_FOR_SHOP_BOT = "پرداخت با روش‌های دیگر فقط در ربات اصلی ممکن است؛ از کیف پول استفاده کنید"
STALE_ORDER = (
    "این سفارش برای یک تمدید قبلی است و دیگر قابل اعمال نیست؛ "
    "با پشتیبانی برای بازگشت وجه هماهنگ کنید."
)


def is_package_order_note(note: str | None) -> bool:
    return (note or "").strip().startswith(NOTE_PREFIX)


def format_note(plan_id: int, generation: int) -> str:
    return f"{NOTE_PREFIX}{int(plan_id)}:{int(generation)}"


def parse_note(note: str | None) -> tuple[int, int] | None:
    """``(plan_id, generation)`` frozen on the order, or None when the note is not ours."""
    match = _NOTE_RE.match((note or "").strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


async def create_package_order(
    session: AsyncSession,
    *,
    owner: BotUser,
    profile: ResellerProfile,
    plan_id: int,
    generation: int,
) -> Order:
    """A pending platform order to renew/upgrade into ``plan_id`` for the generation the page showed."""
    from app.services.pg_admin_subscription import NO_TIME_RENEWAL, plan_has_time

    if not profile.is_active:
        raise ValueError(INVALID_ORDER)
    overview = await package_overview(session, profile)
    if int(overview.generation) != int(generation):
        raise ValueError(STALE_PAGE)
    choice = next((c for c in overview.choices if int(c.plan.id) == int(plan_id)), None)
    if choice is None:
        raise ValueError("بسته پیدا نشد")
    if choice.blocked:
        raise ValueError(choice.blocked)
    if choice.relation == RELATION_CURRENT and not plan_has_time(choice.plan):
        raise ValueError(NO_TIME_RENEWAL)
    amount = int(choice.invoice.get("total") or 0)
    if amount <= 0:
        raise ValueError(INVALID_ORDER)
    note = format_note(plan_id, generation)
    if parse_note(note) is None:
        raise ValueError(INVALID_ORDER)
    order = Order(
        user_id=int(owner.id),
        plan_id=None,
        amount=amount,
        status=OrderStatus.PENDING.value,
        note=note,
        reseller_id=None,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def _set_state(
    session: AsyncSession,
    order_id: int,
    *,
    expected: str,
    status: str,
    pending: bool,
) -> bool:
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == expected)
        .values(status=status, service_mutation_pending=pending)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount == 1)


async def apply_package_order(session: AsyncSession, order: Order) -> Order:
    """Apply a paid package renew/upgrade once; see the module docstring for failure handling."""
    from app.services.pg_admin_subscription import get_subscription
    from app.services.resellers import get_reseller_profile

    parsed = parse_note(order.note)
    if parsed is None or order.reseller_id is not None:
        raise ValueError(INVALID_ORDER)
    plan_id, generation = parsed
    order_id = int(order.id)
    if not await _set_state(
        session,
        order_id,
        expected=OrderStatus.PAID.value,
        status=OrderStatus.DELIVERING.value,
        pending=True,
    ):
        await session.refresh(order)
        if order.status == OrderStatus.DELIVERED.value:
            return order
        raise ValueError("سفارش تمدید بسته قابل پردازش نیست")
    await session.commit()

    try:
        profile = await get_reseller_profile(session, int(order.user_id))
        if profile is None or not profile.is_active:
            raise ValueError(INVALID_ORDER)
        owner = await session.get(BotUser, int(order.user_id))
        if owner is None:
            raise ValueError(INVALID_ORDER)
        uname = (profile.pg_admin_username or "").strip()
        sub = await get_subscription(session, uname) if uname else None
        if sub is None or int(sub.renew_generation or 0) != int(generation):
            raise ValueError(STALE_ORDER)
        await renew_package(
            session,
            owner=owner,
            profile=profile,
            plan_id=plan_id,
            generation=generation,
            charge_wallet=False,
        )
    except Exception:
        await rollback_quiet(session)
        await _set_state(
            session,
            order_id,
            expected=OrderStatus.DELIVERING.value,
            status=OrderStatus.PAID.value,
            pending=False,
        )
        await session.commit()
        raise

    await _set_state(
        session,
        order_id,
        expected=OrderStatus.DELIVERING.value,
        status=OrderStatus.DELIVERED.value,
        pending=False,
    )
    await session.commit()
    await session.refresh(order)
    return order
