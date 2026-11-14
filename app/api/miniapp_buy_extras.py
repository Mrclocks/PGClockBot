"""Mini App purchase extras: the required service name and staff free checkout.

The same rules as the bot's payment screen, through the same services: a shop with
``custom_service_name`` (or the platform with its switch on) requires a name before
paying, and the shop owner with ``free_checkout`` may take their own order free.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order
from app.services.custom_service_name import (
    NAME_REQUIRED,
    name_step_enabled,
    normalize_service_name,
)
from app.services.miniapp_shop import MiniScope
from app.services.redact import user_safe_error
from app.services.staff_free_checkout import deliver_staff_free_order, may_use_staff_free

logger = logging.getLogger(__name__)

STAFF_FREE_MODE = "staff_free"
SERVICE_NAME_MAX_INPUT = 64
DELIVERY_UNDER_REVIEW = (
    "پنل به ساخت سرویس پاسخ نداد. سفارش در «تحویل‌های ناموفق» ثبت شد؛ سفارش دیگری ثبت نکنید."
)
DELIVERY_FAILED = "ساخت سرویس ناموفق بود؛ سفارش لغو شد. دوباره تلاش کنید."


async def checkout_options(session: AsyncSession, user: BotUser, scope: MiniScope) -> dict[str, bool]:
    """What the checkout card shows: a required name field, and the staff free button."""
    return {
        "service_name_required": await name_step_enabled(session, scope.shop_id),
        # The platform Mini App has no commerce for its owner, so only a shop owner gets it.
        "staff_free": scope.is_shop and await may_use_staff_free(session, user, shop_owner_id=scope.shop_id),
    }


def service_name_field(body: dict) -> str | None:
    raw = body.get("service_name")
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > SERVICE_NAME_MAX_INPUT:
        raise HTTPException(400, "نام سرویس نامعتبر است")
    return raw


def is_staff_free_request(body: dict) -> bool:
    return body.get("mode") == STAFF_FREE_MODE


async def check_buy_extras(
    session: AsyncSession, user: BotUser, scope: MiniScope, *, staff_free: bool, service_name: str | None
) -> None:
    """Refuse before an order exists: a missing or invalid required name, or free checkout without the right."""
    if staff_free and not (
        scope.is_shop and await may_use_staff_free(session, user, shop_owner_id=scope.shop_id)
    ):
        raise HTTPException(403, "دریافت رایگان برای شما فعال نیست")
    if not await name_step_enabled(session, scope.shop_id):
        return
    if not (service_name or "").strip():
        raise HTTPException(400, NAME_REQUIRED)
    try:
        normalize_service_name(service_name)
    except ValueError as exc:
        raise HTTPException(400, user_safe_error(exc)) from None


async def deliver_free(session: AsyncSession, order: Order, user: BotUser) -> Order:
    """Take the order free and deliver it now; a failure becomes a clear HTTP error."""
    delivered, stuck, error = await deliver_staff_free_order(session, order, actor_id=int(user.id))
    if delivered is not None:
        return delivered
    logger.warning("mini staff free delivery failed order=%s", order.id, exc_info=error)
    raise HTTPException(502, DELIVERY_UNDER_REVIEW if stuck is not None else DELIVERY_FAILED)
