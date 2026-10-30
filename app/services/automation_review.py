"""Operator queue for automatic purchases that stopped for a manual check.

An automatic renewal or add-on that may have debited the wallet without finishing is parked with
``needs_review``. The scheduler never buys again for that service. This module lists those rows for
one shop and lets an operator clear one only when no money or delivery is still unresolved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    ResellerProfile,
    ServiceAutomation,
    UserService,
    WalletTransaction,
)
from app.services.orders import wallet_purchase_reason
from app.services.service_automation import ACTIONS, claim_automation, release_automation

logger = logging.getLogger(__name__)

QUEUE_LIMIT = 50
NOT_FOUND = "این مورد پیدا نشد"
STATE_WAIT_ORDER = "wait_order"
STATE_WILL_FINISH = "will_finish"
STATE_CLEARABLE = "clearable"
STATE_LABELS = {
    STATE_WAIT_ORDER: "سفارش هنوز تعیین تکلیف نشده؛ ابتدا از بخش تحویل/سفارش‌ها اقدام کنید",
    STATE_WILL_FINISH: "سفارش تحویل شده؛ سامانه خودش تکمیل می‌کند",
    STATE_CLEARABLE: "قابل پاک‌سازی",
}
CLEARABLE_ORDER_STATUSES = frozenset({OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value})


@dataclass(slots=True)
class ReviewItem:
    service_id: int
    username: str
    customer: str
    action: str | None
    order_id: int | None
    order_status: str | None
    amount: int | None
    state: str
    wallet_debited: bool


def review_state(order: Order | None) -> str:
    """What the operator must do next for a parked purchase, from the pending order alone."""
    if order is None or order.status in CLEARABLE_ORDER_STATUSES:
        return STATE_CLEARABLE
    if order.status == OrderStatus.DELIVERED.value:
        return STATE_WILL_FINISH
    return STATE_WAIT_ORDER


def _shop_clause(shop_id: int | None):
    return ServiceAutomation.shop_id.is_(None) if shop_id is None else ServiceAutomation.shop_id == int(shop_id)


async def count_review_queue(session: AsyncSession, *, shop_id: int | None) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(ServiceAutomation).where(ServiceAutomation.needs_review.is_(True), _shop_clause(shop_id))
        )
        or 0
    )


async def _debited_orders(session: AsyncSession, orders: list[Order]) -> set[int]:
    """Ids of the orders whose wallet debit is in the ledger, found in one query."""
    reasons = {wallet_purchase_reason(order): order.id for order in orders}
    if not reasons:
        return set()
    rows = await session.execute(
        select(WalletTransaction.user_id, WalletTransaction.reason).where(
            WalletTransaction.reason.in_(list(reasons)),
            WalletTransaction.amount < 0,
            WalletTransaction.user_id.in_({order.user_id for order in orders}),
        )
    )
    debited = {(user_id, reason) for user_id, reason in rows}
    return {order.id for order in orders if (order.user_id, wallet_purchase_reason(order)) in debited}


async def list_review_queue(
    session: AsyncSession, *, shop_id: int | None, after_service_id: int = 0, limit: int = QUEUE_LIMIT
) -> tuple[list[ReviewItem], int | None]:
    """Parked purchases of one shop in service-id order; returns (items, next cursor)."""
    limit = max(1, min(int(limit), 100))
    rows = (
        await session.execute(
            select(ServiceAutomation, UserService.pg_username, BotUser, Order)
            .join(UserService, UserService.id == ServiceAutomation.service_id)
            .join(BotUser, BotUser.id == UserService.bot_user_id)
            .outerjoin(Order, Order.id == ServiceAutomation.pending_order_id)
            .where(ServiceAutomation.needs_review.is_(True), _shop_clause(shop_id), ServiceAutomation.service_id > int(after_service_id))
            .order_by(ServiceAutomation.service_id)
            .limit(limit + 1)
        )
    ).all()
    next_cursor = int(rows[limit - 1][0].service_id) if len(rows) > limit else None
    rows = rows[:limit]
    debited = await _debited_orders(session, [order for _, _, _, order in rows if order is not None])
    items = [
        ReviewItem(
            service_id=int(row.service_id),
            username=username,
            customer=user.full_name or user.username or str(user.telegram_id),
            action=row.pending_action,
            order_id=int(order.id) if order else row.pending_order_id,
            order_status=order.status if order else None,
            amount=int(order.amount) if order else None,
            state=review_state(order),
            wallet_debited=bool(order and order.id in debited),
        )
        for row, username, user, order in rows
    ]
    return items, next_cursor


async def clear_review(
    session: AsyncSession,
    service_id: int,
    *,
    shop_id: int | None,
    actor: str,
    money_settled: bool,
    disable_automation: bool = False,
) -> None:
    """Un-park one service. Refuses while its order is unresolved, and while a debit is unconfirmed."""
    row = await session.get(ServiceAutomation, int(service_id), populate_existing=True)
    if row is None or row.shop_id != shop_id:
        raise ValueError(NOT_FOUND)
    token = await claim_automation(session, int(service_id))
    if token is None:
        raise ValueError("این سرویس در حال پردازش است؛ کمی بعد تلاش کنید")
    try:
        await _clear_locked(session, row, token, actor, money_settled, disable_automation)
    finally:
        await release_automation(session, int(service_id), token)


async def _clear_locked(
    session: AsyncSession, row: ServiceAutomation, token: str, actor: str, money_settled: bool, disable_automation: bool
) -> None:
    await session.refresh(row)
    if not row.needs_review:
        raise ValueError("این مورد دیگر نیاز به بررسی ندارد")
    order = await session.get(Order, row.pending_order_id, populate_existing=True) if row.pending_order_id else None
    state = review_state(order)
    if state == STATE_WAIT_ORDER:
        raise ValueError(f"ابتدا سفارش #{order.id} را در بخش مالی تعیین تکلیف کنید")
    if state == STATE_WILL_FINISH:
        raise ValueError("سفارش تحویل شده است؛ سامانه خودش آن را تکمیل می‌کند")
    if order is not None and order.id in await _debited_orders(session, [order]) and not money_settled:
        raise ValueError("تأیید کنید که مبلغ کسرشده از کیف مشتری برگردانده شده است")
    values = {"needs_review": False, "pending_order_id": None, "pending_action": None}
    for action in ACTIONS:
        notice = getattr(ServiceAutomation, f"{action}_notice")
        # A cleared service may need the same warning again later.
        values[f"{action}_notice"] = case((notice == "review", None), else_=notice)
        if disable_automation:
            values[f"{action}_enabled"] = False
    cleared = await session.execute(
        update(ServiceAutomation)
        .where(
            ServiceAutomation.service_id == row.service_id,
            ServiceAutomation.lock_token == token,
            ServiceAutomation.needs_review.is_(True),
            or_(ServiceAutomation.pending_order_id.is_(None), ServiceAutomation.pending_order_id == row.pending_order_id),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if cleared.rowcount != 1:
        await session.rollback()
        raise ValueError("وضعیت هم‌زمان تغییر کرد؛ دوباره بررسی کنید")
    await session.commit()
    logger.info("automation review cleared service=%s order=%s by=%s", row.service_id, row.pending_order_id, actor)


async def shops_without_bot_token(session: AsyncSession, *, limit: int = 20) -> list[int]:
    """Shops with automatic purchases switched on but no bot to notify from; their ticks are skipped."""
    active = or_(
        ServiceAutomation.renew_enabled.is_(True),
        ServiceAutomation.duration_enabled.is_(True),
        ServiceAutomation.volume_enabled.is_(True),
        ServiceAutomation.pending_order_id.is_not(None),
    )
    rows = await session.scalars(
        select(ServiceAutomation.shop_id)
        .join(ResellerProfile, ResellerProfile.user_id == ServiceAutomation.shop_id)
        .where(active, ResellerProfile.is_active.is_(True), or_(ResellerProfile.bot_token.is_(None), ResellerProfile.bot_token == ""))
        .group_by(ServiceAutomation.shop_id)
        .order_by(ServiceAutomation.shop_id)
        .limit(max(1, min(int(limit), 100)))
    )
    return [int(shop_id) for shop_id in rows]
