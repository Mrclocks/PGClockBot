"""A shop blocks or unblocks one of its customers in that shop only.

The block lives on the person's membership row for the shop (``bot_user_shops``), so it
never reaches the platform or another shop; the account-wide ``BotUser.is_blocked``
remains the platform owner's. The change is a compare-and-set on the row's current state,
recorded in the staff action log in the same commit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import BotUser, BotUserShop
from app.services.db_safe import rollback_quiet
from app.services.shop_manual_adjust import (
    ManualAdjustError,
    REASON_MAX,
    adjustable_customer,
    manual_adjust_shop,
    parse_reason,
)
from app.services.shop_membership import ensure_member
from app.services.staff_audit import actor_from_staff, add_staff_action

CUSTOMERS_BLOCK_PERMISSION = "customers_block"
BLOCKED_MESSAGE = "دسترسی شما به این فروشگاه مسدود شده است."
ACCOUNT_BLOCKED_MESSAGE = "دسترسی شما مسدود شده است."


def block_shop(staff: dict) -> int:
    """The shop in which ``staff`` may block customers; raises ``PermissionError``."""
    return manual_adjust_shop(staff, CUSTOMERS_BLOCK_PERMISSION, denied="دسترسی مسدودسازی مشتری ندارید")


def may_block(staff: dict) -> bool:
    try:
        block_shop(staff)
    except PermissionError:
        return False
    return True


async def is_blocked_in_shop(session: AsyncSession, bot_user_id: int, shop_id: int | None) -> bool:
    """True when the person is blocked in this shop (never for the platform: it has its own flag)."""
    if not shop_id:
        return False
    found = await session.scalar(
        select(BotUserShop.id)
        .where(
            BotUserShop.bot_user_id == int(bot_user_id),
            BotUserShop.shop_id == int(shop_id),
            BotUserShop.is_blocked.is_(True),
        )
        .limit(1)
    )
    return found is not None


async def blocked_notice(
    session: AsyncSession, user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> str | None:
    """The text for a blocked person on this bot, or None: account-wide first, then this shop."""
    if user.is_blocked:
        return ACCOUNT_BLOCKED_MESSAGE
    if is_reseller_bot and reseller_owner_id and int(user.id) != int(reseller_owner_id):
        if await is_blocked_in_shop(session, int(user.id), int(reseller_owner_id)):
            return BLOCKED_MESSAGE
    return None


def not_blocked_in_shop_clause(shop_id: int | None) -> ColumnElement[bool]:
    """For audience queries over ``BotUser``: leave out people the shop blocked."""
    if not shop_id:
        return BotUser.id.is_not(None)
    return ~exists(
        select(BotUserShop.id).where(
            BotUserShop.bot_user_id == BotUser.id,
            BotUserShop.shop_id == int(shop_id),
            BotUserShop.is_blocked.is_(True),
        )
    )


async def set_shop_block(
    session: AsyncSession, *, staff: dict, customer_id: int, blocked: bool, reason: object
) -> bool:
    """Block or unblock; returns whether the state changed (asking for the current state is a no-op)."""
    shop_id = block_shop(staff)
    clean_reason = parse_reason(reason) if blocked else (" ".join(str(reason or "").split())[:REASON_MAX] or None)
    customer = await adjustable_customer(session, shop_id, customer_id)
    actor = actor_from_staff(staff, shop_id=shop_id)
    try:
        await ensure_member(session, int(customer.id), shop_id)
        changed = await session.execute(
            update(BotUserShop)
            .where(
                BotUserShop.bot_user_id == int(customer.id),
                BotUserShop.shop_id == shop_id,
                BotUserShop.is_blocked.is_(not blocked),
            )
            .values(
                is_blocked=blocked,
                blocked_at=datetime.now(timezone.utc) if blocked else None,
                blocked_reason=clean_reason if blocked else None,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            await session.commit()
            return False
        add_staff_action(
            session, actor, action="customer_block" if blocked else "customer_unblock",
            target_type="customer", target_id=int(customer.id), reason=clean_reason,
        )
        await session.commit()
    except ManualAdjustError:
        await rollback_quiet(session)
        raise
    return True


async def shop_block_state(session: AsyncSession, bot_user_id: int, shop_id: int) -> tuple[bool, str | None]:
    """Whether the person is blocked in the shop, and the reason staff gave."""
    row = (
        await session.execute(
            select(BotUserShop.is_blocked, BotUserShop.blocked_reason).where(
                BotUserShop.bot_user_id == int(bot_user_id), BotUserShop.shop_id == int(shop_id)
            )
        )
    ).first()
    if row is None:
        return False, None
    return bool(row.is_blocked), row.blocked_reason


async def shop_block_form_context(session: AsyncSession, staff: dict, customer: BotUser) -> dict[str, object]:
    """Template values for the customer edit modal: the block form only for staff who may use it."""
    try:
        shop_id = block_shop(staff)
    except PermissionError:
        return {"can_block_in_shop": False}
    blocked, reason = await shop_block_state(session, int(customer.id), shop_id)
    return {"can_block_in_shop": True, "shop_blocked": blocked, "shop_blocked_reason": reason or ""}
