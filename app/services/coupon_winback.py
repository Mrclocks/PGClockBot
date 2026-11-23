"""Win-back coupons: customers who stopped buying get one personal coupon per lapse.

A job runs this for every shop whose ``win_back`` program is on. A customer qualifies when
their last delivered purchase in that shop is older than the program's ``inactive_days``. The
coupon's issue key names that last order, so a customer gets one coupon per lapse however often
the job runs, and a new one only after buying again and lapsing again. Each run is bounded per
shop; notices go through one bot per shop, paced under Telegram's limits, and one failed
customer never stops the rest.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast, exists, func, literal, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, CouponProgram, LoyaltyDiscountEntitlement, Order, OrderStatus
from app.services.coupon_programs import TRIGGER_WIN_BACK, coupon_text, issue_coupon
from app.services.db_safe import rollback_quiet
from app.services.platform_identity import deliverable_telegram_id
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user

logger = logging.getLogger(__name__)

CUSTOMERS_PER_SHOP_RUN = 200
SHOPS_PER_RUN = 500
# Below Telegram's per-bot limit (about 30 messages a second).
SEND_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class Notice:
    user_id: int
    chat_id: int
    text: str


def _issue_key_prefix(shop_key: int) -> str:
    return f"{TRIGGER_WIN_BACK}:{shop_key}:"


async def lapsed_customers(
    session: AsyncSession, *, shop_key: int, inactive_days: int, now: datetime, limit: int = CUSTOMERS_PER_SHOP_RUN,
) -> list[tuple[int, int]]:
    """``(user id, last delivered order id)`` of lapsed customers without a coupon for that lapse."""
    shop_id = shop_key or None
    shop = Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == shop_id
    last = (
        select(
            Order.user_id.label("user_id"),
            func.max(Order.id).label("last_id"),
            func.max(func.coalesce(Order.delivered_at, Order.created_at)).label("last_at"),
        )
        .where(shop, Order.status == OrderStatus.DELIVERED.value, Order.amount > 0)
        .group_by(Order.user_id)
        .subquery()
    )
    issue_key = (
        literal(_issue_key_prefix(shop_key)) + cast(last.c.user_id, String)
        + literal(":") + cast(last.c.last_id, String)
    )
    cutoff = now - timedelta(days=inactive_days)
    rows = await session.execute(
        select(last.c.user_id, last.c.last_id)
        .where(
            last.c.last_at <= cutoff,
            ~exists(select(LoyaltyDiscountEntitlement.id).where(LoyaltyDiscountEntitlement.issue_key == issue_key)),
        )
        .order_by(last.c.user_id)
        .limit(limit)
    )
    return [(int(user_id), int(last_id)) for user_id, last_id in rows.all()]


async def _users(session: AsyncSession, ids: list[int]) -> dict[int, BotUser]:
    if not ids:
        return {}
    return {int(u.id): u for u in (await session.scalars(select(BotUser).where(BotUser.id.in_(ids)))).all()}


async def _issue_for_shop(
    session: AsyncSession, *, shop_key: int, inactive_days: int, now: datetime,
) -> list[Notice]:
    """Issue the shop's coupons; returns the notices to send (plain values a rollback cannot expire)."""
    shop_id = shop_key or None
    lapsed = await lapsed_customers(session, shop_key=shop_key, inactive_days=inactive_days, now=now)
    users = await _users(session, [user_id for user_id, _ in lapsed])
    issued: list[Notice] = []
    for index, (user_id, last_id) in enumerate(lapsed):
        user = users.get(user_id)
        if user is None:
            continue
        try:
            coupon = await issue_coupon(session, shop_id=shop_id, trigger=TRIGGER_WIN_BACK, user=user,
                                        issue_key=f"{_issue_key_prefix(shop_key)}{user_id}:{last_id}")
        except (SQLAlchemyError, ValueError):
            logger.warning("win-back coupon failed shop=%s user=%s", shop_key, user_id, exc_info=True)
            await rollback_quiet(session)
            # The rollback expired the loaded customers; reload the ones still to go in one query.
            users = await _users(session, [uid for uid, _ in lapsed[index + 1:]])
            continue
        if coupon is not None:
            chat_id = deliverable_telegram_id(getattr(user, "telegram_id", None))
            if chat_id is not None:
                issued.append(Notice(user_id=user_id, chat_id=chat_id, text=coupon_text(coupon, TRIGGER_WIN_BACK)))
    return issued


async def _notify_shop(session: AsyncSession, shop_id: int | None, notices: list[Notice]) -> None:
    first = await session.get(BotUser, notices[0].user_id)
    if first is None:
        return
    try:
        bot, should_close = await open_notify_bot_for_user(session, first, shop_id=shop_id)
    except ShopBotUnavailable:
        logger.info("win-back notices skipped: shop bot unavailable shop=%s", shop_id)
        return
    try:
        for notice in notices:
            try:
                await bot.send_message(notice.chat_id, notice.text, parse_mode="HTML")
            except Exception:  # noqa: BLE001 - one undeliverable chat never stops the others; logged
                logger.debug("win-back notice failed user=%s", notice.user_id, exc_info=True)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
    finally:
        if should_close:
            await bot.session.close()


async def issue_winback_coupons(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Issue and announce this run's win-back coupons for every shop; returns how many were issued."""
    now = now or datetime.now(timezone.utc)
    # Plain values: a rollback after one shop must not expire what the loop reads next.
    programs = list((await session.execute(
        select(CouponProgram.shop_key, CouponProgram.inactive_days).where(
            CouponProgram.trigger == TRIGGER_WIN_BACK, CouponProgram.enabled.is_(True),
            CouponProgram.inactive_days > 0,
        ).order_by(CouponProgram.id).limit(SHOPS_PER_RUN)
    )).all())
    total = 0
    for shop_key, inactive_days in programs:
        try:
            issued = await _issue_for_shop(session, shop_key=int(shop_key), inactive_days=int(inactive_days), now=now)
        except SQLAlchemyError:
            logger.warning("win-back run failed shop=%s", shop_key, exc_info=True)
            await rollback_quiet(session)
            continue
        total += len(issued)
        if issued:
            try:
                await _notify_shop(session, shop_key or None, issued)
            except Exception:  # noqa: BLE001 - coupons are stored and shown at checkout; notices are optional
                logger.warning("win-back notices failed shop=%s", shop_key, exc_info=True)
    return total
