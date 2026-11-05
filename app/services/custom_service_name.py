"""The buyer names a new service before paying.

When the shop turns on ``custom_service_name_enabled``, choosing a plan leads to a
required name step (a typed name or «🎲 اسم تصادفی») before the payment screen. The
name is stored on the order and takes the place of the naming pattern's ``{random}``
part at delivery, so the shop's prefix and suffix stay. A name already in use gets a
short random ending without asking the buyer, here and again at delivery if the
panel still holds it (see ``orders.create_with_name_fallback``).
"""

from __future__ import annotations

import re
import secrets
import string

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Plan, UserService
from app.services.orders import (
    CHOSEN_NAME_MAX_LENGTH,
    chosen_name_variant,
    generate_pg_username,
    is_mutation_order_note,
)
from app.services.users import on

SETTING_KEY = "custom_service_name_enabled"
MIN_LENGTH = 3
MAX_LENGTH = CHOSEN_NAME_MAX_LENGTH
# A free variant is found in one or two tries; the bound only stops a pathological loop.
MAX_VARIANT_TRIES = 8
RANDOM_NAME_LENGTH = 8
_NAME_RE = re.compile(rf"^[a-z][a-z0-9_]{{{MIN_LENGTH - 1},{MAX_LENGTH - 1}}}$")
_EDITABLE_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.REJECTED.value,
    OrderStatus.AWAITING_RECEIPT.value,
)
_HELD_STATUSES = (*_EDITABLE_STATUSES, OrderStatus.PAID.value, OrderStatus.DELIVERING.value)

NAME_RULES = (
    f"فقط حروف انگلیسی کوچک، عدد و _ ؛ بین {MIN_LENGTH} تا {MAX_LENGTH} نویسه و با یک حرف شروع شود."
)
ORDER_LOCKED = "نام این سفارش دیگر قابل تغییر نیست."
NO_FREE_NAME = "برای این نام جای خالی پیدا نشد؛ نام دیگری بفرستید."


def offers_custom_name(ui: dict, order: Order | None) -> bool:
    """Whether this order goes through the name step."""
    return (
        on(ui.get(SETTING_KEY))
        and isinstance(order, Order)
        and order.plan_id is not None
        and int(order.quantity or 1) == 1
        and not is_mutation_order_note(order.note)
        and order.status in _EDITABLE_STATUSES
    )


def needs_service_name(ui: dict, order: Order | None) -> bool:
    """The name step is required and has not been answered yet."""
    return offers_custom_name(ui, order) and not order.requested_username


def normalize_service_name(raw: str | None) -> str:
    """Validated lower-case name."""
    text = (raw or "").strip().lstrip("@").lower()
    if not _NAME_RE.fullmatch(text):
        raise ValueError(NAME_RULES)
    return text


def random_service_name() -> str:
    """A name for «🎲 اسم تصادفی»: a letter, then letters and digits."""
    rest = string.ascii_lowercase + string.digits
    return secrets.choice(string.ascii_lowercase) + "".join(
        secrets.choice(rest) for _ in range(RANDOM_NAME_LENGTH - 1)
    )


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
        .where(Order.requested_username == name, Order.id != int(order.id), shop, Order.status.in_(_HELD_STATUSES))
        .limit(1)
    )
    return claimed is not None


async def _free_name(session: AsyncSession, order: Order, wanted: str) -> tuple[str, str]:
    """``wanted`` if free, else the first free variant with a random ending; returns (name, username)."""
    name = wanted
    for _ in range(MAX_VARIANT_TRIES):
        username = await preview_username(session, order, name)
        if not await _name_in_use(session, order, name, username):
            return name, username
        name = chosen_name_variant(wanted)
    raise ValueError(NO_FREE_NAME)


async def set_service_name(session: AsyncSession, order: Order, *, buyer_id: int, raw: str | None) -> str:
    """Store the name on the buyer's own unpaid order; returns the panel username.

    A name already in use silently gets a short random ending.
    """
    name, username = await _free_name(session, order, normalize_service_name(raw))
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
