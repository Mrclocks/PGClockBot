"""Purchase funnel: record a customer's step and summarise the steps for a shop."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FunnelEvent,
    Order,
    OrderStatus,
)
from app.services.demo_users import non_demo_customer

logger = logging.getLogger(__name__)


FUNNEL_STEPS = (
    "shop_open",
    "plan_view",
    "pay_start",
    "receipt",
    "delivered",
)


async def record_funnel_event(
    session: AsyncSession,
    *,
    step: str,
    user_id: int | None = None,
    reseller_id: int | None = None,
    plan_id: int | None = None,
    order_id: int | None = None,
    idempotency_key: str | None = None,
) -> None:
    step = (step or "").strip()
    if step not in FUNNEL_STEPS:
        return
    key = (idempotency_key or "").strip()
    if not key:
        key = f"{step}:{user_id or 0}:{plan_id or 0}:{order_id or 0}:{int(datetime.now(timezone.utc).timestamp())}"
    existing = (
        await session.execute(select(FunnelEvent.id).where(FunnelEvent.idempotency_key == key))
    ).scalar_one_or_none()
    if existing:
        return
    session.add(
        FunnelEvent(
            step=step,
            user_id=int(user_id) if user_id else None,
            reseller_id=int(reseller_id) if reseller_id else None,
            plan_id=int(plan_id) if plan_id else None,
            order_id=int(order_id) if order_id else None,
            idempotency_key=key[:128],
        )
    )
    try:
        # A savepoint, so a lost race on the unique key leaves the caller's transaction alone.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        logger.debug("funnel event %s already recorded", key, exc_info=True)


async def funnel_summary(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
    days: int = 7,
    window: tuple[datetime, datetime] | None = None,
) -> dict[str, int]:
    """Step counts for the behavior panel: the last ``days`` days, or the half-open ``window``."""
    since, until = window or (datetime.now(timezone.utc) - timedelta(days=max(1, min(90, days))), None)
    q = select(FunnelEvent.step, func.count()).where(
        FunnelEvent.created_at >= since, non_demo_customer(FunnelEvent.user_id),
    )
    if until is not None:
        q = q.where(FunnelEvent.created_at < until)
    if reseller_id is None:
        q = q.where(FunnelEvent.reseller_id.is_(None))
    else:
        q = q.where(FunnelEvent.reseller_id == int(reseller_id))
    q = q.group_by(FunnelEvent.step)
    rows = (await session.execute(q)).all()
    out: dict[str, int] = {s: 0 for s in FUNNEL_STEPS}
    for step, cnt in rows:
        if step in out:
            out[step] = int(cnt or 0)
    # Fallback derive from orders when funnel empty
    if not any(out.values()):
        oq = select(Order.status, func.count()).where(
            Order.created_at >= since, non_demo_customer(Order.user_id),
        )
        if until is not None:
            oq = oq.where(Order.created_at < until)
        if reseller_id is None:
            oq = oq.where(Order.reseller_id.is_(None))
        else:
            oq = oq.where(Order.reseller_id == int(reseller_id))
        oq = oq.group_by(Order.status)
        for st, cnt in (await session.execute(oq)).all():
            n = int(cnt or 0)
            if st in {
                OrderStatus.PENDING.value,
                OrderStatus.AWAITING_RECEIPT.value,
                OrderStatus.AWAITING_APPROVAL.value,
                OrderStatus.PAID.value,
                OrderStatus.DELIVERING.value,
                OrderStatus.DELIVERED.value,
            }:
                out["pay_start"] += n
            if st in {
                OrderStatus.AWAITING_APPROVAL.value,
                OrderStatus.PAID.value,
                OrderStatus.DELIVERING.value,
                OrderStatus.DELIVERED.value,
            }:
                out["receipt"] += n
            if st == OrderStatus.DELIVERED.value:
                out["delivered"] += n
    return out
