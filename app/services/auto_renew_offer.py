"""Offer automatic renewal once a customer has renewed a service by hand twice.

A customer who keeps renewing manually is the one most likely to be cut off by a late payment.
After the second delivered manual renewal of a service, the customer is sent one notice with a
button to the service's automatic-renewal settings. The offer is made at most once per service:
it is claimed by setting ``ServiceAutomation.renew_offered_at`` where it is still NULL, in the
same commit that queues the notice, so a repeated delivery hook or two renewals at once cannot
send it twice. The notice goes through the throttled customer-notice queue, after the renewal
itself is committed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, ServiceAutomation, UserService
from app.services.customer_notice_queue import add_notice
from app.services.db_safe import rollback_quiet
from app.services.service_scope import service_shop_id
from app.services.users import get_all_settings, on

logger = logging.getLogger(__name__)

ACTION = "auto_renew_offer"
SETTING_KEY = "auto_renew_offer_enabled"
MANUAL_RENEWALS_BEFORE_OFFER = 2
RENEW_NOTE_PREFIX = "renew:"
AUTO_NOTE_SUFFIX = ":auto"
OFFER_TEXT = (
    "💡 <b>تمدید خودکار</b>\n"
    "سرویس <code>{name}</code> را چند بار دستی تمدید کرده‌اید. با روشن کردن تمدید خودکار، "
    "هنگام تمام شدن سرویس، تمدید از کیف پول انجام می‌شود و سرویس قطع نمی‌شود. "
    "هر زمان بخواهید می‌توانید آن را خاموش کنید."
)


def is_manual_renewal(order: Order) -> bool:
    note = (order.note or "").strip()
    return note.startswith(RENEW_NOTE_PREFIX) and not note.endswith(AUTO_NOTE_SUFFIX)


async def _manual_renewal_count(session: AsyncSession, service_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(Order.id)).where(
                Order.service_id == service_id,
                Order.status == OrderStatus.DELIVERED.value,
                Order.note.like(f"{RENEW_NOTE_PREFIX}%"),
                Order.note.not_like(f"%{AUTO_NOTE_SUFFIX}"),
            )
        )
        or 0
    )


async def _ensure_automation_row(session: AsyncSession, service: UserService) -> None:
    """The row the claim is written to; created without any purchase switched on."""
    # Deferred: service_automation imports the order services that call this module.
    from app.services.service_automation import get_automation

    await get_automation(session, service)


async def _claim_offer(session: AsyncSession, service_id: int) -> bool:
    result = await session.execute(
        update(ServiceAutomation)
        .where(
            ServiceAutomation.service_id == service_id,
            ServiceAutomation.renew_offered_at.is_(None),
            ServiceAutomation.renew_enabled.is_(False),
        )
        .values(renew_offered_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def _is_offer_due(session: AsyncSession, order: Order, service: UserService) -> bool:
    if not is_manual_renewal(order) or not service.pg_user_id or (service.remark or "").strip() == "linked":
        return False
    ui = await get_all_settings(session, reseller_id=int(order.reseller_id) if order.reseller_id else 0)
    if not on(ui.get(SETTING_KEY, "1")) or not on(ui.get("pay_wallet_enabled", "1")):
        return False
    return await _manual_renewal_count(session, int(service.id)) >= MANUAL_RENEWALS_BEFORE_OFFER


async def queue_auto_renew_offer(session: AsyncSession, order: Order) -> bool:
    """Queue the one-time offer after a delivered manual renewal; True when it was queued.

    Best effort: a failure is logged and never touches the renewal, which is already committed.
    """
    try:
        service = await session.get(UserService, int(order.service_id)) if order.service_id else None
        user = await session.get(BotUser, int(order.user_id))
        if service is None or user is None or user.is_blocked or int(service.bot_user_id) != int(user.id):
            return False
        if not await _is_offer_due(session, order, service):
            return False
        await _ensure_automation_row(session, service)
        if not await _claim_offer(session, int(service.id)):
            await session.rollback()
            return False
        add_notice(
            session,
            shop_id=await service_shop_id(session, service),
            bot_user_id=int(user.id),
            telegram_id=int(user.telegram_id),
            action=ACTION,
            service_name=service.pg_username or "",
            reason="",
            service_id=int(service.id),
        )
        await session.commit()
        return True
    except SQLAlchemyError:
        logger.warning("auto-renew offer not queued order=%s", order.id, exc_info=True)
        await rollback_quiet(session)
        return False
