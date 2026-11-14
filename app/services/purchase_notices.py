"""Tell staff about a purchase made outside the bot, where no live bot is at hand.

A wallet purchase in the bot announces itself through the bot that took it. A Mini App
purchase has no such bot, so the order's own bot (the shop's, or the platform's) is
opened for the notice and closed again. Each delivered order is announced once: the
notice is claimed on the order first, so a retried request or a second tap that gets
the same order back sends nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, Plan
from app.services.db_safe import rollback_quiet
from app.services.notifications import notify_new_subscription

logger = logging.getLogger(__name__)


async def _claim_notice(session: AsyncSession, order_id: int) -> bool:
    """Mark a delivered order as announced; False when it is not delivered or already was."""
    claim = await session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status == OrderStatus.DELIVERED.value,
            Order.staff_notified_at.is_(None),
        )
        .values(staff_notified_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return claim.rowcount == 1


async def _send_notice(session: AsyncSession, *, order: Order, user: BotUser) -> None:
    # Local import: reseller_bots pulls in the bot dispatcher, as in trial_notify.
    from app.services.reseller_bots import open_notify_bot_for_user

    bot, should_close = await open_notify_bot_for_user(session, user, shop_id=order.reseller_id)
    try:
        plan_name = (
            await session.scalar(select(Plan.name).where(Plan.id == int(order.plan_id)))
            if order.plan_id
            else None
        )
        await notify_new_subscription(
            bot, session, order=order, user_tg_id=user.telegram_id, plan_name=plan_name
        )
    finally:
        if should_close:
            await bot.session.close()


async def notify_purchase_outside_bot(session: AsyncSession, *, order: Order, user: BotUser) -> None:
    """Send the «اشتراک جدید» / «تمدید» notice for a delivered order, once; best effort.

    The purchase is already committed, so nothing here may turn it into an error:
    a shop without a bot, a bad token, a Telegram or database failure is logged and
    the session is left usable. The claim is committed before Telegram is called, so
    a notice that fails to send is not retried.
    """
    from app.services.reseller_bots import ShopBotUnavailable  # Local import, as in _send_notice.

    # Read before sending: a rollback after a database error expires the order.
    order_id = int(order.id)
    try:
        if not await _claim_notice(session, order_id):
            return
        await _send_notice(session, order=order, user=user)
    except ShopBotUnavailable:
        logger.warning("purchase notice: no shop bot order=%s", order_id)
    except SQLAlchemyError:
        await rollback_quiet(session)
        logger.warning("purchase notice failed order=%s", order_id, exc_info=True)
    except Exception:
        # Last resort after a committed purchase: report it and keep the purchase.
        logger.exception("purchase notice failed order=%s", order_id)
