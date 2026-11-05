"""The buyer picks the name of a new service at checkout.

When the shop turns on ``custom_service_name_enabled``, the payment screen offers
«✏️ نام دلخواه سرویس». The name is stored on the order and takes the place of the
naming pattern's ``{random}`` part at delivery, so the shop's prefix and suffix stay.
A name the panel already holds falls back to a generated one at delivery (see
``orders.create_with_name_fallback``), so a paid order never waits on it.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Plan, UserService
from app.services.orders import generate_pg_username, is_mutation_order_note
from app.services.users import on

SETTING_KEY = "custom_service_name_enabled"
MIN_LENGTH = 3
MAX_LENGTH = 20
CLEAR_WORD = "-"
_NAME_RE = re.compile(rf"^[a-z][a-z0-9_]{{{MIN_LENGTH - 1},{MAX_LENGTH - 1}}}$")
_EDITABLE_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.REJECTED.value,
    OrderStatus.AWAITING_RECEIPT.value,
)

NAME_RULES = (
    f"فقط حروف انگلیسی کوچک، عدد و _ ؛ بین {MIN_LENGTH} تا {MAX_LENGTH} نویسه و با یک حرف شروع شود."
)
NAME_TAKEN = "این نام قبلاً استفاده شده است؛ نام دیگری بفرستید."
ORDER_LOCKED = "نام این سفارش دیگر قابل تغییر نیست."


def offers_custom_name(ui: dict, order: Order | None) -> bool:
    """Whether the payment screen offers a name for this order."""
    return (
        on(ui.get(SETTING_KEY))
        and isinstance(order, Order)
        and order.plan_id is not None
        and int(order.quantity or 1) == 1
        and not is_mutation_order_note(order.note)
        and order.status in _EDITABLE_STATUSES
    )


def normalize_service_name(raw: str | None) -> str | None:
    """Validated lower-case name, or None when the buyer asked to clear it."""
    text = (raw or "").strip().lstrip("@").lower()
    if text == CLEAR_WORD:
        return None
    if not _NAME_RE.fullmatch(text):
        raise ValueError(NAME_RULES)
    return text


async def preview_username(session: AsyncSession, order: Order, name: str) -> str:
    """The panel username delivery will try for this order with ``name``."""
    plan = await session.get(Plan, int(order.plan_id))
    return await generate_pg_username(
        session,
        user_id=int(order.id),
        buyer_id=int(order.user_id),
        plan=plan,
        reseller_id=order.reseller_id,
        chosen_name=name,
    )


async def _name_in_use(session: AsyncSession, order: Order, name: str, username: str) -> bool:
    """Another service already has this username, or another open order in the shop chose the name."""
    taken = await session.scalar(
        select(UserService.id).where(func.lower(UserService.pg_username) == username.lower()).limit(1)
    )
    if taken is not None:
        return True
    shop = Order.reseller_id.is_(None) if order.reseller_id is None else Order.reseller_id == int(order.reseller_id)
    claimed = await session.scalar(
        select(Order.id)
        .where(
            Order.requested_username == name,
            Order.id != int(order.id),
            shop,
            Order.status.in_((*_EDITABLE_STATUSES, OrderStatus.PAID.value, OrderStatus.DELIVERING.value)),
        )
        .limit(1)
    )
    return claimed is not None


async def set_service_name(
    session: AsyncSession, order: Order, *, buyer_id: int, raw: str | None
) -> str | None:
    """Store (or clear) the chosen name on the buyer's own unpaid order; returns the panel username."""
    name = normalize_service_name(raw)
    username = None
    if name is not None:
        username = await preview_username(session, order, name)
        if await _name_in_use(session, order, name, username):
            raise ValueError(NAME_TAKEN)
    result = await session.execute(
        update(Order)
        .where(
            Order.id == int(order.id),
            Order.user_id == int(buyer_id),
            Order.status.in_(_EDITABLE_STATUSES),
        )
        .values(requested_username=name)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError(ORDER_LOCKED)
    await session.commit()
    await session.refresh(order)
    return username
