"""Purchase-funnel steps recorded from the order flow, for every channel (bot, Mini App, panel).

The funnel page counts events per shop, so each step is stored with the shop of the order itself.
Recording never changes an order: a failure is logged and the order path carries on.
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order
from app.services.db_safe import rollback_quiet
from app.services.users import get_setting, on
from app.services.ux20 import record_funnel_event

logger = logging.getLogger(__name__)

STEP_PAY_START = "pay_start"
STEP_RECEIPT = "receipt"
STEP_DELIVERED = "delivered"


async def record_order_funnel(session: AsyncSession, order: Order, step: str, *, key: str) -> None:
    """Record ``step`` once per ``key`` for the order's shop, honoring that shop's tracking switch."""
    try:
        if not on(await get_setting(session, "funnel_tracking_enabled", "1", reseller_id=order.reseller_id)):
            return
        await record_funnel_event(
            session,
            step=step,
            user_id=order.user_id,
            reseller_id=order.reseller_id,
            plan_id=order.plan_id,
            order_id=order.id,
            idempotency_key=key,
        )
        await session.commit()
    except SQLAlchemyError:
        logger.warning("funnel step %s not recorded for order %s", step, order.id, exc_info=True)
        await rollback_quiet(session)
    except Exception:
        # Analytics must never fail a payment or a delivery that already succeeded; nothing was
        # written, so the caller's objects are left as they are.
        logger.warning("funnel step %s not recorded for order %s", step, order.id, exc_info=True)
