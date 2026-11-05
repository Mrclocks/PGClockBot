"""Staff take a new service order for themselves without paying.

The platform owner (main bot) and a shop's operators (its own bot, with the
``free_checkout`` shop permission) can finish their own purchase from the payment
screen instead of sending a receipt to themselves. The order is claimed with a
zero amount and its price moved to ``discount_amount``, so revenue reports and
referral bonuses ignore it. Delivery runs the normal path: the shop's panel
account, its volume ceiling and its quotas still apply.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentMethod, PaymentStatus
from app.services.bot_principal_identity import resolve_bot_org_principal
from app.services.org_principals import is_owner_principal
from app.services.orders import (
    DeliveryAttempt,
    deliver_order,
    is_mutation_order_note,
    settle_failed_free_delivery,
)
from app.services.reseller_access import resolve_reseller_owner_id
from app.services.resellers import get_reseller_profile, has_bot_perm

STAFF_FREE_PERMISSION = "free_checkout"
STAFF_FREE_METHOD = PaymentMethod.STAFF_FREE.value
NOT_ELIGIBLE = "این سفارش برای دریافت رایگان قابل استفاده نیست"

_CLAIMABLE_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.REJECTED.value,
    OrderStatus.AWAITING_RECEIPT.value,
)


def is_new_service_order(order: Order | None) -> bool:
    """Plain, custom or wholesale purchase; renewals, add-ons and shop capacity are excluded."""
    return isinstance(order, Order) and order.plan_id is not None and not is_mutation_order_note(order.note)


async def _is_shop_operator_with_permission(
    session: AsyncSession, actor: BotUser, reseller_owner_id: int | None
) -> bool:
    owner_id = await resolve_reseller_owner_id(
        session, actor, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        return False
    profile = await get_reseller_profile(session, int(owner_id))
    return bool(profile and profile.is_active and has_bot_perm(profile, STAFF_FREE_PERMISSION))


async def _is_platform_owner(session: AsyncSession, actor: BotUser) -> bool:
    principal = await resolve_bot_org_principal(session, db_user=actor, is_reseller_bot=False)
    return principal is not None and is_owner_principal(principal)


async def may_use_staff_free(
    session: AsyncSession, actor: BotUser, *, shop_owner_id: int | None
) -> bool:
    """Whether this person may take their own new orders free where they are buying.

    ``shop_owner_id`` is the shop whose bot or Mini App is in use (None: the platform).
    """
    if shop_owner_id is not None:
        return await _is_shop_operator_with_permission(session, actor, shop_owner_id)
    return await _is_platform_owner(session, actor)


async def may_take_order_free(
    session: AsyncSession,
    actor: BotUser,
    order: Order | None,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> bool:
    """Whether this person may finish their own order for free in the bot they are using."""
    if not is_new_service_order(order) or int(order.user_id) != int(actor.id):
        return False
    if order.status not in _CLAIMABLE_STATUSES:
        return False
    shop_owner_id = reseller_owner_id if is_reseller_bot else None
    if int(order.reseller_id or 0) != int(shop_owner_id or 0):
        return False
    return await may_use_staff_free(session, actor, shop_owner_id=shop_owner_id)


async def claim_staff_free_order(session: AsyncSession, order: Order, *, actor_id: int) -> Order:
    """Atomically mark the order paid at zero and record who took it."""
    if not is_new_service_order(order):
        raise ValueError(NOT_ELIGIBLE)
    order_id = int(order.id)
    with session.no_autoflush:
        claim = await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(_CLAIMABLE_STATUSES))
            .values(
                status=OrderStatus.PAID.value,
                payment_method=STAFF_FREE_METHOD,
                discount_amount=Order.discount_amount + Order.amount,
                amount=0,
            )
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        raise ValueError("این سفارش دیگر قابل دریافت نیست")
    # An earlier card/gateway attempt must not be approvable once the order is taken free.
    await session.execute(
        update(Payment)
        .where(Payment.order_id == order_id, Payment.status == PaymentStatus.PENDING.value)
        .values(status=PaymentStatus.REJECTED.value, review_note="superseded by staff free checkout")
        .execution_options(synchronize_session=False)
    )
    session.add(
        Payment(
            order_id=order_id,
            user_id=int(order.user_id),
            amount=0,
            method=STAFF_FREE_METHOD,
            status=PaymentStatus.APPROVED.value,
            review_note=f"staff free checkout by user {int(actor_id)}",
        )
    )
    await session.commit()
    await session.refresh(order)
    return order


async def deliver_staff_free_order(
    session: AsyncSession, order: Order, *, actor_id: int
) -> tuple[Order | None, Payment | None, Exception | None]:
    """Claim and deliver; returns ``(delivered, stuck_payment, error)``.

    When delivery fails and the panel created nothing, the order is cancelled (the
    person starts a new purchase). When the panel may hold the account, the order
    stays paid and is listed under failed deliveries; its payment is returned so
    staff can be told.
    """
    order = await claim_staff_free_order(session, order, actor_id=actor_id)
    attempt = DeliveryAttempt()
    try:
        return await deliver_order(session, order, attempt=attempt), None, None
    except Exception as exc:
        # Every delivery failure is settled (cancelled or queued for staff) and returned.
        stuck = await settle_failed_free_delivery(
            session, order, attempt=attempt, error=str(exc), reopen=False
        )
        return None, stuck, exc

