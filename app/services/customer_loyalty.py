"""The customer's club outside the bot chat (Mini App): points, tier, invites and rewards.

Everything here is one club's purse: ``shop_id`` is the shop whose Mini App asks (None
for the platform), and points, rewards, discounts and services are that shop's only.
Redemption goes through
``loyalty.redeem_reward`` with the bot's slot key, so a double tap, a retried request or
the same reward redeemed from the bot at once still spends the points once.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, LoyaltyDiscountEntitlement, LoyaltyReward, PointsTransaction
from app.services.db_safe import rollback_quiet
from app.services.loyalty import (
    REWARD_TYPE_LABELS,
    TX_TYPE_LABELS,
    get_tier_for_points,
    list_active_rewards,
    list_available_discounts,
    loyalty_enabled,
    month_earned_points,
    next_redemption_key,
    points_balance,
    redeem_reward,
    referral_link,
    referral_stats,
)
from app.services.service_scope import list_customer_services
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

logger = logging.getLogger(__name__)

HISTORY_PAGE_SIZE = 10
REWARD_LIST_LIMIT = 30
SERVICE_CHOICE_LIMIT = 10
SERVICE_REWARD_TYPES = frozenset({"traffic_gb", "time_days"})
# A panel call that failed without a clear answer may still have added the traffic or days.
UNKNOWN_OUTCOME_MESSAGE = (
    "نتیجهٔ اعمال جایزه روی سرویس مشخص نشد. پیش از تلاش دوباره سرویس را بررسی کنید "
    "یا با پشتیبانی تماس بگیرید."
)


class ClubError(ValueError):
    """A club rule the customer must see (Persian, safe to show)."""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _reward_card(reward: LoyaltyReward, points: int) -> dict:
    cost = int(reward.points_cost or 0)
    return {
        "id": int(reward.id),
        "name": reward.name or "",
        "description": (reward.description or "").strip(),
        "type": reward.reward_type,
        "type_label": REWARD_TYPE_LABELS.get(reward.reward_type, ""),
        "value": int(reward.reward_value or 0),
        "cost": cost,
        "needs_service": reward.reward_type in SERVICE_REWARD_TYPES,
        "affordable": 0 < cost <= points,
    }


def _discount_card(ent: LoyaltyDiscountEntitlement) -> dict:
    return {
        "code": ent.code,
        "percent": int(ent.percent or 0),
        "min_purchase": int(ent.min_purchase_toman or 0),
        "max_discount": int(ent.max_discount_toman) if ent.max_discount_toman is not None else None,
        "expires_at": _iso(ent.expires_at),
    }


def history_entry(tx: PointsTransaction) -> dict:
    return {
        "id": int(tx.id),
        "amount": int(tx.amount),
        "label": (tx.description or "").strip() or TX_TYPE_LABELS.get(tx.tx_type, ""),
        "created_at": _iso(tx.created_at),
    }


async def points_history_page(
    session: AsyncSession, user_id: int, *, shop_id: int | None, before_id: int | None = None,
    limit: int = HISTORY_PAGE_SIZE,
) -> tuple[list[dict], bool]:
    """One page of the customer's points entries in this club, newest first (keyset on id)."""
    purse = PointsTransaction.reseller_id.is_(None) if shop_id is None else PointsTransaction.reseller_id == int(shop_id)
    query = select(PointsTransaction).where(PointsTransaction.user_id == int(user_id), purse)
    if before_id:
        query = query.where(PointsTransaction.id < int(before_id))
    rows = list(await session.scalars(query.order_by(PointsTransaction.id.desc()).limit(limit + 1)))
    return [history_entry(tx) for tx in rows[:limit]], len(rows) > limit


async def _referral(session: AsyncSession, user: BotUser, bot_username: str) -> dict:
    stats = await referral_stats(session, int(user.id))
    code = user.referral_code or ""
    return {
        "code": code,
        "link": referral_link(bot_username, code) if bot_username and code else "",
        "invited": int(stats["qualified"] or stats["total"]),
        "earned_points": int(stats["earned_points"]),
    }


async def count_affordable_rewards(session: AsyncSession, user: BotUser, *, shop_id: int | None) -> int:
    """Rewards in this club the customer can take now; 0 while the club is off."""
    if not await loyalty_enabled(session, reseller_id=shop_id):
        return 0
    points = await points_balance(session, user, shop_id)
    if points <= 0:
        return 0
    club = LoyaltyReward.reseller_id.is_(None) if shop_id is None else LoyaltyReward.reseller_id == int(shop_id)
    count = await session.scalar(
        select(func.count(LoyaltyReward.id)).where(
            club,
            LoyaltyReward.enabled.is_(True),
            LoyaltyReward.archived.is_(False),
            LoyaltyReward.points_cost > 0,
            LoyaltyReward.points_cost <= points,
        )
    )
    return int(count or 0)


async def club_summary(session: AsyncSession, user: BotUser, *, shop_id: int | None, bot_username: str) -> dict:
    """What the club screen shows; points and rewards only while the club is on.

    ``bot_username`` is the club's own bot, so an invite link brings people to that bot.
    """
    referral = await _referral(session, user, bot_username)
    if not await loyalty_enabled(session, reseller_id=shop_id):
        return {"enabled": False, "referral": referral}
    points = await points_balance(session, user, shop_id)
    tier = await get_tier_for_points(session, points)
    rewards = (await list_active_rewards(session, reseller_id=shop_id))[:REWARD_LIST_LIMIT]
    history, has_more = await points_history_page(session, int(user.id), shop_id=shop_id)
    services: list[dict] = []
    if any(r.reward_type in SERVICE_REWARD_TYPES for r in rewards):
        services = [
            {"id": int(s.id), "name": s.pg_username or f"سرویس #{s.id}"}
            for s in await list_customer_services(session, int(user.id), shop_id, limit=SERVICE_CHOICE_LIMIT)
        ]
    return {
        "enabled": True,
        "points": points,
        "tier": {"name": tier.name, "points_to_next": tier.points_to_next},
        "month_points": await month_earned_points(session, int(user.id), shop_id=shop_id),
        "referral": referral,
        "rewards": [_reward_card(r, points) for r in rewards],
        "services": services,
        "discounts": [
            _discount_card(d) for d in await list_available_discounts(session, int(user.id), shop_id=shop_id)
        ],
        "history": history,
        "history_more": has_more,
    }


async def _club_reward(session: AsyncSession, reward_id: int, shop_id: int | None) -> LoyaltyReward:
    reward = await session.get(LoyaltyReward, int(reward_id))
    reward_shop = int(reward.reseller_id) if reward is not None and reward.reseller_id else None
    if reward is None or reward_shop != shop_id or not reward.enabled or reward.archived:
        raise ClubError("این جایزه فعال نیست")
    return reward


async def redeem_club_reward(
    session: AsyncSession, user: BotUser, *, shop_id: int | None, reward_id: int, service_id: int | None,
) -> dict:
    """Spend this club's points on one of its rewards; the same tap or retry never spends twice."""
    if not await loyalty_enabled(session, reseller_id=shop_id):
        raise ClubError("باشگاه مشتریان فعلاً غیرفعال است")
    reward = await _club_reward(session, reward_id, shop_id)
    needs_service = reward.reward_type in SERVICE_REWARD_TYPES
    if needs_service and not service_id:
        raise ClubError("یک سرویس فعال انتخاب کنید")
    target_service = int(service_id) if needs_service else None
    user_id = int(user.id)
    key = await next_redemption_key(session, user_id, int(reward.id), target_service)
    # redeem_reward reads the shop from the request context; pin it to this club.
    token = set_shop_reseller_id(shop_id)
    try:
        redemption = await redeem_reward(
            session, user, int(reward.id), service_id=target_service, idempotency_key=key,
        )
    except ValueError as exc:
        await rollback_quiet(session)
        raise ClubError(str(exc) or "بازخرید ناموفق بود") from exc
    except OperationalError as exc:
        # SQLite refuses a second concurrent writer; the debit rolled back with it.
        await rollback_quiet(session)
        raise ClubError("سرور مشغول است؛ چند ثانیه بعد دوباره تلاش کنید") from exc
    except Exception as exc:
        await rollback_quiet(session)
        if not needs_service:
            raise
        logger.warning("club reward outcome unknown user=%s reward=%s", user_id, reward_id, exc_info=True)
        raise ClubError(UNKNOWN_OUTCOME_MESSAGE) from exc
    finally:
        reset_shop_reseller_id(token)
    await session.refresh(user)
    return {
        "reward_type": redemption.reward_type,
        "reward_value": int(redemption.reward_value or 0),
        "discount_code": redemption.discount_code or None,
        "points": await points_balance(session, user, shop_id),
    }
