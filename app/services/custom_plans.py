"""Per-order custom plans: hiding them from catalog lists and discarding unsold ones.

``create_custom_order`` stores each buyer's chosen volume and days on its own inactive
``Plan`` so delivery can read them. Those rows are an implementation detail of the order,
not catalog entries an operator manages.
"""

from __future__ import annotations

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Plan, UserService

CUSTOM_ORDER_NOTE = "custom"
_UNSOLD_STATUSES = (OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value)


def exclude_custom_order_plans(query: Select) -> Select:
    """Drop the per-order plans of custom orders from a ``select(Plan)`` listing."""
    custom_plan_ids = select(Order.plan_id).where(
        Order.note == CUSTOM_ORDER_NOTE, Order.plan_id.is_not(None)
    )
    return query.where(Plan.id.not_in(custom_plan_ids))


async def discard_unsold_custom_plan(session: AsyncSession, order: Order) -> bool:
    """Delete the per-order plan of a cancelled/rejected custom order; True when removed.

    Reads the status from the database (callers update it with a bulk UPDATE) and keeps
    the plan if any other order or service still points at it. Does not commit.
    """
    plan_id = order.plan_id
    if order.note != CUSTOM_ORDER_NOTE or not plan_id:
        return False
    status = await session.scalar(select(Order.status).where(Order.id == order.id))
    if status not in _UNSOLD_STATUSES:
        return False
    plan = await session.get(Plan, int(plan_id))
    if plan is None or plan.is_active or plan.is_trial:
        return False
    other_orders = await session.scalar(
        select(func.count()).select_from(Order).where(Order.plan_id == plan.id, Order.id != order.id)
    )
    services = await session.scalar(
        select(func.count()).select_from(UserService).where(UserService.plan_id == plan.id)
    )
    if other_orders or services:
        return False
    await session.execute(
        update(Order).where(Order.id == order.id).values(plan_id=None).execution_options(synchronize_session=False)
    )
    order.plan_id = None
    await session.delete(plan)
    return True
