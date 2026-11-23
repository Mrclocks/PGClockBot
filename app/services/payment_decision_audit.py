"""Staff log records for payment decisions: approving or rejecting a receipt, and a free checkout.

The record is added to the caller's open transaction, so it is committed with the claim that made
the decision and never exists without it. It belongs to the shop of the payment (a top-up's purse,
else its order's shop), which is the shop whose log shows it. Automatic approvals (no reviewer) are
not staff actions and get no record.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, Order, Payment
from app.services.shop_scope import payment_shop_id
from app.services.staff_audit import (
    ACTOR_PLATFORM_OWNER,
    ACTOR_SHOP_OWNER,
    ACTOR_STAFF,
    LABEL_MAX,
    StaffActor,
    add_staff_action,
)

ACTION_APPROVE = "payment_approve"
ACTION_REJECT = "payment_reject"
ACTION_FREE_CHECKOUT = "free_checkout"


def _label(user: BotUser | None, telegram_id: int) -> str:
    if user is not None and (user.full_name or "").strip():
        return user.full_name.strip()[:LABEL_MAX]
    if user is not None and (user.username or "").strip():
        return f"@{user.username.strip()}"[:LABEL_MAX]
    return str(int(telegram_id))


def _kind_for(user_id: int | None, telegram_id: int, shop_id: int | None) -> str:
    if shop_id is not None and user_id is not None and int(user_id) == int(shop_id):
        return ACTOR_SHOP_OWNER
    if shop_id is None and int(telegram_id) in {int(t) for t in get_settings().admin_ids or []}:
        return ACTOR_PLATFORM_OWNER
    return ACTOR_STAFF


async def bot_reviewer_actor(session: AsyncSession, telegram_id: int | None, *, shop_id: int | None) -> StaffActor | None:
    """The actor behind a bot decision, from the reviewer's Telegram id; None for an automatic one."""
    if not telegram_id or int(telegram_id) <= 0:
        return None
    user = await session.scalar(select(BotUser).where(BotUser.telegram_id == int(telegram_id)).limit(1))
    user_id = int(user.id) if user is not None else None
    return StaffActor(
        shop_id=shop_id,
        kind=_kind_for(user_id, int(telegram_id), shop_id),
        user_id=user_id,
        label=_label(user, int(telegram_id)),
    )


async def record_payment_decision(
    session: AsyncSession,
    payment: Payment,
    order: Order | None,
    *,
    action: str,
    reviewer_tg: int | None,
    actor: StaffActor | None = None,
) -> None:
    """Add the decision to the staff log of the payment's shop; the caller's commit writes it.

    ``actor`` comes from a web session; without it the bot reviewer's Telegram id identifies them.
    """
    shop_id = payment_shop_id(payment, order)
    resolved = replace(actor, shop_id=shop_id) if actor is not None else await bot_reviewer_actor(
        session, reviewer_tg, shop_id=shop_id
    )
    if resolved is None:
        return
    add_staff_action(
        session,
        resolved,
        action=action,
        target_type="payment",
        target_id=int(payment.id),
        amount=int(payment.amount or 0),
    )


async def record_free_checkout(session: AsyncSession, order: Order, *, actor_id: int) -> None:
    """A staff member took a new-service order without payment; logged against that order."""
    user = await session.get(BotUser, int(actor_id))
    shop_id = int(order.reseller_id) if order.reseller_id else None
    telegram_id = int(user.telegram_id or 0) if user is not None else 0
    actor = StaffActor(
        shop_id=shop_id,
        kind=_kind_for(int(actor_id), telegram_id, shop_id),
        user_id=int(actor_id),
        label=_label(user, telegram_id) if user is not None else f"#{int(actor_id)}",
    )
    add_staff_action(session, actor, action=ACTION_FREE_CHECKOUT, target_type="order", target_id=int(order.id))
