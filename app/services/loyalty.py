"""Referral + Loyalty / Points engine.

Extends existing referral_code / referred_by_id / referral_bonus wallet credit.
Points ledger is the audit source of truth; BotUser.points_balance is the platform purse and ShopPoints holds each shop's.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    LoyaltyDiscountEntitlement,
    LoyaltyReward,
    LoyaltyTier,
    Order,
    Plan,
    PointsRule,
    PointsTransaction,
    ReferralEvent,
    RewardRedemption,
    ShopPoints,
    UserService,
)
from app.services.demo_users import non_demo_customer
from app.services.formatting import bot_user_panel_label
from app.services.service_scope import customer_may_use_service, service_shop_id

logger = logging.getLogger(__name__)

# Human-friendly Persian labels for admin UI (never expose raw keys as primary labels)
EVENT_LABELS: dict[str, str] = {
    "purchase": "خرید موفق",
    "first_purchase": "اولین خرید کاربر",
    "renewal": "تمدید موفق",
    "referral_signup": "ثبت‌نام با دعوت",
    "referral_first_purchase": "اولین خرید دعوت‌شده",
    "referral_purchase": "خرید دعوت‌شده",
    "referral_qualification": "واجد شرایط شدن دعوت",
}

REWARD_TYPE_LABELS: dict[str, str] = {
    "traffic_gb": "ترافیک (گیگ)",
    "time_days": "زمان (روز)",
    "wallet_credit": "اعتبار کیف پول",
    "discount_percent": "تخفیف درصدی",
}

TX_TYPE_LABELS: dict[str, str] = {
    "earn": "دریافت",
    "spend": "مصرف",
    "adjust": "تعدیل ادمین",
    "reverse": "برگشت",
    "redeem": "بازخرید جایزه",
}

DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "name": "خرید موفق (به‌ازای هر گیگ)",
        "event_key": "purchase",
        "amount_mode": "per_gb",
        "amount": 1,
        "enabled": True,
        "sort_order": 10,
    },
    {
        "name": "اولین خرید کاربر",
        "event_key": "first_purchase",
        "amount_mode": "fixed",
        "amount": 50,
        "enabled": True,
        "first_time_only": True,
        "sort_order": 20,
    },
    {
        "name": "تمدید موفق",
        "event_key": "renewal",
        "amount_mode": "fixed",
        "amount": 20,
        "enabled": True,
        "sort_order": 30,
    },
    {
        "name": "ثبت‌نام با دعوت",
        "event_key": "referral_signup",
        "amount_mode": "fixed",
        "amount": 0,
        "enabled": True,
        "sort_order": 40,
    },
    {
        "name": "اولین خرید دعوت‌شده",
        "event_key": "referral_first_purchase",
        "amount_mode": "fixed",
        "amount": 50,
        "enabled": True,
        "first_time_only": True,
        "sort_order": 50,
    },
    {
        "name": "خرید دعوت‌شده",
        "event_key": "referral_purchase",
        "amount_mode": "fixed",
        "amount": 10,
        "enabled": True,
        "sort_order": 60,
    },
]

DEFAULT_TIERS: list[dict[str, Any]] = [
    {"name": "برنز", "min_points": 0, "max_points": 999, "sort_order": 1, "multiplier_bps": 10000},
    {"name": "نقره", "min_points": 1000, "max_points": 4999, "sort_order": 2, "multiplier_bps": 10000},
    {"name": "طلا", "min_points": 5000, "max_points": None, "sort_order": 3, "multiplier_bps": 10000},
]

DEFAULT_REWARDS: list[dict[str, Any]] = [
    {
        "name": "۵ گیگ رایگان",
        "description": "افزودن ۵ گیگ به یکی از سرویس‌های فعال",
        "reward_type": "traffic_gb",
        "reward_value": 5,
        "points_cost": 100,
        "sort_order": 10,
    },
    {
        "name": "۱۰ گیگ رایگان",
        "description": "افزودن ۱۰ گیگ به یکی از سرویس‌های فعال",
        "reward_type": "traffic_gb",
        "reward_value": 10,
        "points_cost": 180,
        "sort_order": 20,
    },
    {
        "name": "۳۰ روز زمان",
        "description": "افزودن ۳۰ روز به یکی از سرویس‌های فعال",
        "reward_type": "time_days",
        "reward_value": 30,
        "points_cost": 250,
        "sort_order": 30,
    },
    {
        "name": "۵۰٬۰۰۰ اعتبار کیف پول",
        "description": "واریز اعتبار به کیف پول (غیرقابل برداشت نقدی)",
        "reward_type": "wallet_credit",
        "reward_value": 50000,
        "points_cost": 500,
        "sort_order": 40,
    },
    {
        "name": "۱۵٪ تخفیف خرید",
        "description": "کد تخفیف یک‌بارمصرف برای خرید بعدی",
        "reward_type": "discount_percent",
        "reward_value": 15,
        "points_cost": 300,
        "sort_order": 50,
        "min_purchase_toman": 0,
        "max_discount_toman": 100000,
        "expires_days": 7,
    },
]

SETTING_LOYALTY_ENABLED = "loyalty_enabled"
SETTING_POINTS_TO_WALLET_RATE = "points_to_wallet_rate"  # toman per 1 point when converting via wallet_credit rewards


@dataclass(frozen=True)
class TierInfo:
    name: str
    min_points: int
    max_points: int | None
    points_to_next: int | None
    multiplier_bps: int


async def ensure_loyalty_defaults(session: AsyncSession, *, reseller_id: int | None = None) -> None:
    """Seed default rules/tiers/rewards once (idempotent)."""
    from app.services.users import get_setting, set_setting

    enabled = await get_setting(session, SETTING_LOYALTY_ENABLED, "1", reseller_id=reseller_id)
    if enabled == "":
        await set_setting(session, SETTING_LOYALTY_ENABLED, "1", reseller_id=reseller_id)
    rate = await get_setting(session, SETTING_POINTS_TO_WALLET_RATE, "", reseller_id=reseller_id)
    if rate == "":
        await set_setting(session, SETTING_POINTS_TO_WALLET_RATE, "100", reseller_id=reseller_id)

    q: Select = select(func.count()).select_from(PointsRule)
    if reseller_id is None:
        q = q.where(PointsRule.reseller_id.is_(None))
    else:
        q = q.where(PointsRule.reseller_id == int(reseller_id))
    n = int((await session.execute(q)).scalar_one() or 0)
    if n == 0:
        for row in DEFAULT_RULES:
            session.add(
                PointsRule(
                    name=row["name"],
                    event_key=row["event_key"],
                    amount_mode=row["amount_mode"],
                    amount=int(row["amount"]),
                    enabled=bool(row.get("enabled", True)),
                    first_time_only=bool(row.get("first_time_only", False)),
                    sort_order=int(row.get("sort_order", 0)),
                    reseller_id=reseller_id,
                )
            )

    tn = int((await session.execute(select(func.count()).select_from(LoyaltyTier))).scalar_one() or 0)
    if tn == 0:
        for row in DEFAULT_TIERS:
            session.add(
                LoyaltyTier(
                    name=row["name"],
                    min_points=int(row["min_points"]),
                    max_points=row["max_points"],
                    sort_order=int(row["sort_order"]),
                    multiplier_bps=int(row["multiplier_bps"]),
                    enabled=True,
                )
            )

    rq: Select = select(func.count()).select_from(LoyaltyReward)
    if reseller_id is None:
        rq = rq.where(LoyaltyReward.reseller_id.is_(None))
    else:
        rq = rq.where(LoyaltyReward.reseller_id == int(reseller_id))
    rn = int((await session.execute(rq)).scalar_one() or 0)
    if rn == 0:
        for row in DEFAULT_REWARDS:
            session.add(
                LoyaltyReward(
                    name=row["name"],
                    description=row.get("description"),
                    reward_type=row["reward_type"],
                    reward_value=int(row["reward_value"]),
                    points_cost=int(row["points_cost"]),
                    enabled=True,
                    archived=False,
                    sort_order=int(row.get("sort_order", 0)),
                    reseller_id=reseller_id,
                    min_purchase_toman=int(row.get("min_purchase_toman") or 0),
                    max_discount_toman=row.get("max_discount_toman"),
                    expires_days=row.get("expires_days"),
                )
            )
    await session.commit()


async def loyalty_enabled(session: AsyncSession, *, reseller_id: int | None = None) -> bool:
    from app.services.users import get_setting, on

    raw = await get_setting(session, SETTING_LOYALTY_ENABLED, "1", reseller_id=reseller_id)
    return on(raw)


def referral_link(bot_username: str, code: str) -> str:
    uname = (bot_username or "bot").lstrip("@")
    return f"https://t.me/{uname}?start=ref_{code}"


async def record_referral_event(
    session: AsyncSession,
    *,
    referrer_id: int,
    referred_id: int,
    event_key: str,
    source: str = "telegram",
    order_id: int | None = None,
    qualification_state: str = "pending",
    reward_state: str = "none",
    idempotency_key: str,
    meta: dict | None = None,
) -> ReferralEvent | None:
    if referrer_id == referred_id:
        return None
    existing = (
        await session.execute(
            select(ReferralEvent).where(ReferralEvent.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    ev = ReferralEvent(
        referrer_id=int(referrer_id),
        referred_id=int(referred_id),
        event_key=event_key,
        source=source,
        status="recorded",
        qualification_state=qualification_state,
        reward_state=reward_state,
        order_id=order_id,
        idempotency_key=idempotency_key,
        meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
    )
    session.add(ev)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return (
            await session.execute(
                select(ReferralEvent).where(ReferralEvent.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
    return ev


async def points_balance(session: AsyncSession, user: BotUser, shop_id: int | None) -> int:
    """The customer's points in one shop (None = the platform's ``BotUser.points_balance``)."""
    if not shop_id:
        return int(user.points_balance or 0)
    balance = await session.scalar(
        select(ShopPoints.balance).where(ShopPoints.user_id == int(user.id), ShopPoints.reseller_id == int(shop_id))
    )
    return int(balance or 0)


async def _ensure_shop_points_row(session: AsyncSession, user_id: int, shop_id: int) -> None:
    if await session.scalar(
        select(ShopPoints.id).where(ShopPoints.user_id == user_id, ShopPoints.reseller_id == shop_id)
    ):
        return
    try:
        async with session.begin_nested():
            session.add(ShopPoints(user_id=user_id, reseller_id=shop_id, balance=0))
            await session.flush()
    except IntegrityError:
        logger.debug("shop points row already created user=%s shop=%s", user_id, shop_id)


async def _apply_points_delta(
    session: AsyncSession, user: BotUser, shop_id: int | None, delta: int, *, allow_negative: bool = False
) -> int:
    """Atomically move one purse by ``delta`` and return its new balance."""
    with session.no_autoflush:
        if shop_id:
            await _ensure_shop_points_row(session, int(user.id), int(shop_id))
            cond = [ShopPoints.user_id == int(user.id), ShopPoints.reseller_id == int(shop_id)]
            if delta < 0 and not allow_negative:
                cond.append(ShopPoints.balance >= -delta)
            result = await session.execute(
                update(ShopPoints).where(*cond).values(balance=ShopPoints.balance + delta)
                .execution_options(synchronize_session=False)
            )
        else:
            cond = [BotUser.id == int(user.id)]
            if delta < 0 and not allow_negative:
                cond.append(BotUser.points_balance >= -delta)
            result = await session.execute(
                update(BotUser).where(*cond).values(points_balance=BotUser.points_balance + delta)
                .execution_options(synchronize_session=False)
            )
    if result.rowcount != 1:
        if delta < 0:
            raise ValueError("امتیاز کافی نیست")
        raise ValueError("کاربر یافت نشد")
    await session.refresh(user)
    return await points_balance(session, user, shop_id)


async def credit_points(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    *,
    tx_type: str,
    source: str,
    reference: str | None,
    description: str,
    idempotency_key: str,
    meta: dict | None = None,
    created_by: str | None = None,
    commit: bool = True,
    shop_id: int | None,
) -> PointsTransaction | None:
    """Credit one purse (``shop_id`` None = platform) idempotently; returns the existing tx on a repeat key."""
    if amount == 0:
        return None
    if amount < 0:
        raise ValueError("credit_points amount must be >= 0")
    existing = (
        await session.execute(
            select(PointsTransaction).where(PointsTransaction.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    balance_after = await _apply_points_delta(session, user, shop_id, int(amount))
    tx = PointsTransaction(
        user_id=user.id,
        amount=int(amount),
        balance_after=balance_after,
        reseller_id=int(shop_id) if shop_id else None,
        tx_type=tx_type,
        source=source,
        reference=reference,
        description=description,
        idempotency_key=idempotency_key,
        meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
        created_by=created_by,
    )
    session.add(tx)
    try:
        if commit:
            await session.commit()
            await session.refresh(user)
            await session.refresh(tx)
        else:
            await session.flush()
    except IntegrityError:
        await session.rollback()
        return (
            await session.execute(
                select(PointsTransaction).where(
                    PointsTransaction.idempotency_key == idempotency_key
                )
            )
        ).scalar_one_or_none()
    return tx


async def debit_points(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    *,
    tx_type: str,
    source: str,
    reference: str | None,
    description: str,
    idempotency_key: str,
    meta: dict | None = None,
    created_by: str | None = None,
    commit: bool = True,
    allow_negative: bool = False,
    shop_id: int | None,
) -> PointsTransaction | None:
    """Debit one purse (``shop_id`` None = platform); no negative balance unless allow_negative (debt policy)."""
    if amount <= 0:
        raise ValueError("debit_points amount must be positive")
    existing = (
        await session.execute(
            select(PointsTransaction).where(PointsTransaction.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    balance_after = await _apply_points_delta(session, user, shop_id, -int(amount), allow_negative=allow_negative)
    tx = PointsTransaction(
        user_id=user.id,
        amount=-int(amount),
        balance_after=balance_after,
        reseller_id=int(shop_id) if shop_id else None,
        tx_type=tx_type,
        source=source,
        reference=reference,
        description=description,
        idempotency_key=idempotency_key,
        meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
        created_by=created_by,
    )
    session.add(tx)
    try:
        if commit:
            await session.commit()
            await session.refresh(user)
            await session.refresh(tx)
        else:
            await session.flush()
    except IntegrityError:
        await session.rollback()
        return (
            await session.execute(
                select(PointsTransaction).where(
                    PointsTransaction.idempotency_key == idempotency_key
                )
            )
        ).scalar_one_or_none()
    return tx


async def reverse_points_tx(
    session: AsyncSession,
    original: PointsTransaction,
    *,
    reason: str,
    created_by: str | None = None,
    commit: bool = True,
) -> PointsTransaction | None:
    """Reverse a prior earn/spend. Idempotent via reverse:{original.id}."""
    key = f"reverse:{int(original.id)}"
    existing = (
        await session.execute(
            select(PointsTransaction).where(PointsTransaction.idempotency_key == key)
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    amt = int(original.amount)
    if amt == 0:
        return None
    user = await session.get(BotUser, original.user_id)
    if not user:
        return None
    if amt > 0:
        # Original was credit → debit back; allow negative (spent-points debt policy)
        return await debit_points(
            session,
            user,
            amt,
            tx_type="reverse",
            source=original.source,
            reference=original.reference,
            description=reason,
            idempotency_key=key,
            meta={"reversed_tx_id": original.id},
            created_by=created_by,
            commit=commit,
            allow_negative=True,
            shop_id=original.reseller_id,
        )
    return await credit_points(
        session,
        user,
        -amt,
        tx_type="reverse",
        source=original.source,
        reference=original.reference,
        description=reason,
        idempotency_key=key,
        meta={"reversed_tx_id": original.id},
        created_by=created_by,
        commit=commit,
        shop_id=original.reseller_id,
    )


async def admin_adjust_points(
    session: AsyncSession,
    user: BotUser,
    delta: int,
    *,
    reason: str,
    admin_identity: str,
    shop_id: int | None,
) -> PointsTransaction:
    if not reason or not str(reason).strip():
        raise ValueError("دلیل الزامی است")
    if delta == 0:
        raise ValueError("مقدار نمی‌تواند صفر باشد")
    key = f"admin_adjust:{user.id}:{admin_identity}:{int(datetime.now(timezone.utc).timestamp())}:{delta}:{reason.strip()[:40]}"
    prev = await points_balance(session, user, shop_id)
    if delta > 0:
        tx = await credit_points(
            session,
            user,
            delta,
            tx_type="adjust",
            source="admin",
            reference=None,
            description=reason.strip(),
            idempotency_key=key,
            meta={"previous_balance": prev},
            created_by=admin_identity,
            commit=True,
            shop_id=shop_id,
        )
    else:
        tx = await debit_points(
            session,
            user,
            -delta,
            tx_type="adjust",
            source="admin",
            reference=None,
            description=reason.strip(),
            idempotency_key=key,
            meta={"previous_balance": prev},
            created_by=admin_identity,
            commit=True,
            allow_negative=False,
            shop_id=shop_id,
        )
    if tx is None:
        raise ValueError("تعدیل انجام نشد")
    return tx


async def get_tier_for_points(session: AsyncSession, points: int) -> TierInfo:
    rows = list(
        (
            await session.execute(
                select(LoyaltyTier)
                .where(LoyaltyTier.enabled.is_(True))
                .order_by(LoyaltyTier.min_points.asc(), LoyaltyTier.sort_order.asc())
            )
        ).scalars().all()
    )
    if not rows:
        return TierInfo("برنز", 0, None, None, 10000)
    current = rows[0]
    for t in rows:
        if int(points) >= int(t.min_points):
            current = t
    nxt = None
    for t in rows:
        if int(t.min_points) > int(current.min_points):
            nxt = t
            break
    to_next = None
    if nxt is not None:
        to_next = max(0, int(nxt.min_points) - int(points))
    return TierInfo(
        name=current.name,
        min_points=int(current.min_points),
        max_points=int(current.max_points) if current.max_points is not None else None,
        points_to_next=to_next,
        multiplier_bps=int(current.multiplier_bps or 10000),
    )


def _plan_gb(plan: Plan | None) -> float:
    if not plan or plan.data_limit_gb is None:
        return 0.0
    try:
        return float(plan.data_limit_gb)
    except (TypeError, ValueError):
        return 0.0


async def _rules_for_event(
    session: AsyncSession,
    event_key: str,
    *,
    reseller_id: int | None,
) -> list[PointsRule]:
    q = (
        select(PointsRule)
        .where(
            PointsRule.event_key == event_key,
            PointsRule.enabled.is_(True),
        )
        .order_by(PointsRule.sort_order.asc(), PointsRule.id.asc())
    )
    if reseller_id is None:
        q = q.where(PointsRule.reseller_id.is_(None))
    else:
        q = q.where(PointsRule.reseller_id == int(reseller_id))
    rows = list((await session.execute(q)).scalars().all())
    if not rows and reseller_id is not None:
        # Fall back to platform rules
        q2 = (
            select(PointsRule)
            .where(
                PointsRule.event_key == event_key,
                PointsRule.enabled.is_(True),
                PointsRule.reseller_id.is_(None),
            )
            .order_by(PointsRule.sort_order.asc(), PointsRule.id.asc())
        )
        rows = list((await session.execute(q2)).scalars().all())
    return rows


async def _rule_passes(
    session: AsyncSession,
    rule: PointsRule,
    *,
    user: BotUser,
    order: Order | None,
    plan: Plan | None,
    gb: float,
    balance: int,
) -> bool:
    if rule.plan_id and order and order.plan_id and int(rule.plan_id) != int(order.plan_id):
        return False
    if rule.plan_id and not order:
        return False
    amount = int(order.amount) if order else 0
    if int(rule.min_purchase_toman or 0) > 0 and amount < int(rule.min_purchase_toman):
        return False
    if int(rule.min_purchase_gb or 0) > 0 and gb < float(rule.min_purchase_gb):
        return False
    if int(rule.tier_min_points or 0) > 0 and balance < int(rule.tier_min_points):
        return False
    if rule.first_time_only:
        prior = (
            await session.execute(
                select(PointsTransaction.id)
                .where(
                    PointsTransaction.user_id == user.id,
                    PointsTransaction.source == f"rule:{rule.id}",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior is not None:
            return False
    if int(rule.cooldown_hours or 0) > 0:
        since = datetime.now(timezone.utc) - timedelta(hours=int(rule.cooldown_hours))
        recent = (
            await session.execute(
                select(PointsTransaction.id)
                .where(
                    PointsTransaction.user_id == user.id,
                    PointsTransaction.source == f"rule:{rule.id}",
                    PointsTransaction.created_at >= since,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if recent is not None:
            return False
    return True


def _compute_rule_amount(rule: PointsRule, *, gb: float, multiplier_bps: int) -> int:
    if rule.amount_mode == "per_gb":
        raw = int(rule.amount) * int(gb)
    else:
        raw = int(rule.amount)
    if raw <= 0:
        return 0
    scaled = (raw * int(multiplier_bps)) // 10000
    if rule.max_reward is not None:
        scaled = min(scaled, int(rule.max_reward))
    return max(0, int(scaled))


async def apply_event_rules(
    session: AsyncSession,
    *,
    user: BotUser,
    event_key: str,
    idempotency_prefix: str,
    order: Order | None = None,
    plan: Plan | None = None,
    reseller_id: int | None = None,
    description_override: str | None = None,
) -> list[PointsTransaction]:
    if not await loyalty_enabled(session, reseller_id=reseller_id):
        return []
    rules = await _rules_for_event(session, event_key, reseller_id=reseller_id)
    if not rules:
        return []
    gb = _plan_gb(plan)
    balance = await points_balance(session, user, reseller_id)
    tier = await get_tier_for_points(session, balance)
    out: list[PointsTransaction] = []
    for rule in rules:
        if not await _rule_passes(session, rule, user=user, order=order, plan=plan, gb=gb, balance=balance):
            continue
        pts = _compute_rule_amount(rule, gb=gb, multiplier_bps=tier.multiplier_bps)
        if pts <= 0:
            continue
        key = f"{idempotency_prefix}:rule:{rule.id}"
        tx = await credit_points(
            session,
            user,
            pts,
            tx_type="earn",
            source=f"rule:{rule.id}",
            reference=str(order.id) if order else None,
            description=description_override or rule.name,
            idempotency_key=key,
            meta={"event": event_key, "rule_id": rule.id},
            commit=True,
            shop_id=reseller_id,
        )
        if tx:
            out.append(tx)
    return out


async def on_user_referred(
    session: AsyncSession,
    *,
    referred: BotUser,
    source: str = "telegram",
) -> None:
    if not referred.referred_by_id:
        return
    if int(referred.referred_by_id) == int(referred.id):
        referred.referred_by_id = None
        await session.commit()
        return
    try:
        await ensure_loyalty_defaults(session, reseller_id=referred.reseller_id)
    except Exception:
        pass
    await record_referral_event(
        session,
        referrer_id=int(referred.referred_by_id),
        referred_id=int(referred.id),
        event_key="referral_signup",
        source=source,
        qualification_state="pending",
        idempotency_key=f"ref_signup:{referred.id}",
    )
    referrer = await session.get(BotUser, int(referred.referred_by_id))
    if referrer:
        await apply_event_rules(
            session,
            user=referrer,
            event_key="referral_signup",
            idempotency_prefix=f"ref_signup:{referred.id}",
            reseller_id=referred.reseller_id,
            description_override="ثبت‌نام با دعوت",
        )


async def _is_first_delivered_purchase(session: AsyncSession, order: Order) -> bool:
    note = (order.note or "").strip()
    if note.startswith("renew:") or note.startswith("reseller_app:"):
        return False
    prior = (
        await session.execute(
            select(Order.id)
            .where(
                Order.user_id == order.user_id,
                Order.status == "delivered",
                Order.id != order.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return prior is None


async def on_order_delivered(session: AsyncSession, order: Order) -> None:
    """Award purchase / referral points after successful delivery. Idempotent."""
    note = (order.note or "").strip()
    if note.startswith("reseller_app:"):
        return
    # Prefer already-loaded relationships; never trigger async lazy-load (xd2s).
    user = order.__dict__.get("user")
    if user is None:
        user = await session.get(BotUser, order.user_id)
    if not user:
        return
    plan = order.__dict__.get("plan")
    if plan is None and order.plan_id:
        plan = await session.get(Plan, order.plan_id)
    reseller_id = order.reseller_id
    try:
        await ensure_loyalty_defaults(session, reseller_id=reseller_id)
    except Exception:
        logger.exception("ensure_loyalty_defaults failed")

    is_renew = note.startswith("renew:")
    if is_renew:
        await apply_event_rules(
            session,
            user=user,
            event_key="renewal",
            idempotency_prefix=f"order:{order.id}:renewal",
            order=order,
            plan=plan,
            reseller_id=reseller_id,
            description_override="تمدید موفق",
        )
        return

    first = await _is_first_delivered_purchase(session, order)
    await apply_event_rules(
        session,
        user=user,
        event_key="purchase",
        idempotency_prefix=f"order:{order.id}:purchase",
        order=order,
        plan=plan,
        reseller_id=reseller_id,
        description_override="خرید موفق",
    )
    if first:
        await apply_event_rules(
            session,
            user=user,
            event_key="first_purchase",
            idempotency_prefix=f"order:{order.id}:first_purchase",
            order=order,
            plan=plan,
            reseller_id=reseller_id,
            description_override="اولین خرید",
        )

    if user.referred_by_id:
        referrer = await session.get(BotUser, int(user.referred_by_id))
        if not referrer or int(referrer.id) == int(user.id):
            return
        # Qualify on first purchase
        await record_referral_event(
            session,
            referrer_id=int(referrer.id),
            referred_id=int(user.id),
            event_key="referral_qualification" if first else "referral_purchase",
            source="order",
            order_id=int(order.id),
            qualification_state="qualified" if first else "qualified",
            reward_state="pending",
            idempotency_key=f"ref_qual:{user.id}" if first else f"ref_purchase:{order.id}",
        )
        if first:
            await apply_event_rules(
                session,
                user=referrer,
                event_key="referral_first_purchase",
                idempotency_prefix=f"ref_first:{user.id}:order:{order.id}",
                order=order,
                plan=plan,
                reseller_id=reseller_id,
                description_override="اولین خرید دعوت‌شده",
            )
            await apply_event_rules(
                session,
                user=referrer,
                event_key="referral_qualification",
                idempotency_prefix=f"ref_qual_pts:{user.id}",
                order=order,
                plan=plan,
                reseller_id=reseller_id,
                description_override="واجد شرایط شدن دعوت",
            )
        else:
            await apply_event_rules(
                session,
                user=referrer,
                event_key="referral_purchase",
                idempotency_prefix=f"ref_purchase:{order.id}",
                order=order,
                plan=plan,
                reseller_id=reseller_id,
                description_override="خرید دعوت‌شده",
            )


async def reverse_order_points(session: AsyncSession, order: Order, *, reason: str) -> int:
    """Reverse all earn txs tied to this order reference. Returns count reversed."""
    rows = list(
        (
            await session.execute(
                select(PointsTransaction).where(
                    PointsTransaction.reference == str(order.id),
                    PointsTransaction.amount > 0,
                    PointsTransaction.tx_type == "earn",
                )
            )
        ).scalars().all()
    )
    n = 0
    for tx in rows:
        try:
            await reverse_points_tx(session, tx, reason=reason, commit=True)
            n += 1
        except Exception:
            logger.exception("reverse points failed tx=%s", tx.id)
    return n


async def list_points_history(
    session: AsyncSession, user_id: int, *, shop_id: int | None, limit: int = 10, offset: int = 0
) -> list[PointsTransaction]:
    """The customer's points entries in one shop (None = platform)."""
    scope = PointsTransaction.reseller_id == int(shop_id) if shop_id else PointsTransaction.reseller_id.is_(None)
    result = await session.execute(
        select(PointsTransaction)
        .where(PointsTransaction.user_id == user_id, scope)
        .order_by(PointsTransaction.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def referral_stats(session: AsyncSession, user_id: int) -> dict[str, int]:
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(BotUser).where(BotUser.referred_by_id == user_id)
            )
        ).scalar_one()
        or 0
    )
    qualified = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ReferralEvent)
                .where(
                    ReferralEvent.referrer_id == user_id,
                    ReferralEvent.qualification_state == "qualified",
                )
            )
        ).scalar_one()
        or 0
    )
    earned = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(PointsTransaction.amount), 0)).where(
                    PointsTransaction.user_id == user_id,
                    PointsTransaction.amount > 0,
                    PointsTransaction.source.like("rule:%"),
                    PointsTransaction.description.like("%دعوت%"),
                )
            )
        ).scalar_one()
        or 0
    )
    # Broader: sum referral event-related earn txs
    if earned == 0:
        earned = int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(PointsTransaction.amount), 0)).where(
                        PointsTransaction.user_id == user_id,
                        PointsTransaction.amount > 0,
                        PointsTransaction.idempotency_key.like("ref_%"),
                    )
                )
            ).scalar_one()
            or 0
        )
    return {"total": total, "qualified": qualified, "earned_points": earned}


async def month_earned_points(session: AsyncSession, user_id: int, *, shop_id: int | None) -> int:
    """Points the customer earned this month in one purse (None = platform)."""
    purse = PointsTransaction.reseller_id == int(shop_id) if shop_id else PointsTransaction.reseller_id.is_(None)
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(
        (
            await session.execute(
                select(func.coalesce(func.sum(PointsTransaction.amount), 0)).where(
                    PointsTransaction.user_id == user_id,
                    PointsTransaction.amount > 0,
                    PointsTransaction.created_at >= start,
                    purse,
                )
            )
        ).scalar_one()
        or 0
    )


async def list_active_rewards(
    session: AsyncSession, *, reseller_id: int | None = None
) -> list[LoyaltyReward]:
    q = (
        select(LoyaltyReward)
        .where(
            LoyaltyReward.enabled.is_(True),
            LoyaltyReward.archived.is_(False),
        )
        .order_by(LoyaltyReward.sort_order.asc(), LoyaltyReward.id.asc())
    )
    if reseller_id is None:
        q = q.where(LoyaltyReward.reseller_id.is_(None))
    else:
        q = q.where(LoyaltyReward.reseller_id == int(reseller_id))
    return list((await session.execute(q)).scalars().all())


async def _user_redemption_count(session: AsyncSession, user_id: int, reward_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(RewardRedemption)
                .where(
                    RewardRedemption.user_id == user_id,
                    RewardRedemption.reward_id == reward_id,
                    RewardRedemption.status == "completed",
                )
            )
        ).scalar_one()
        or 0
    )


async def redeem_reward(
    session: AsyncSession,
    user: BotUser,
    reward_id: int,
    *,
    service_id: int | None = None,
    idempotency_key: str | None = None,
) -> RewardRedemption:
    """Atomic redeem: lock reward + user, debit points, apply benefit, record redemption."""
    from app.services.users import current_shop_reseller_id  # users imports loyalty lazily; avoid a cycle

    key = idempotency_key or f"redeem:{user.id}:{reward_id}:{int(datetime.now(timezone.utc).timestamp())}"
    existing = (
        await session.execute(
            select(RewardRedemption).where(RewardRedemption.idempotency_key == key)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    # Lock reward row when supported (SQLite ignores / may no-op)
    try:
        reward = (
            await session.execute(
                select(LoyaltyReward).where(LoyaltyReward.id == int(reward_id)).with_for_update()
            )
        ).scalar_one_or_none()
    except Exception:
        reward = await session.get(LoyaltyReward, int(reward_id))
    if not reward or not reward.enabled or reward.archived:
        raise ValueError("این جایزه فعال نیست")
    # Scope isolation: a reward is redeemable only in the bot of the shop that owns it.
    user_rid = current_shop_reseller_id()
    # Points, reward and benefit all belong to one shop: a platform reward is not redeemable in
    # a shop bot (its credit would come from the platform's wallet while the points are the shop's).
    if (reward.reseller_id or None) != (user_rid or None):
        raise ValueError("این جایزه فعال نیست")
    if reward.max_redemptions_global is not None and int(reward.redemption_count) >= int(
        reward.max_redemptions_global
    ):
        raise ValueError("سقف کل بازخرید این جایزه پر شده")
    if reward.max_redemptions_per_user is not None:
        used = await _user_redemption_count(session, user.id, reward.id)
        if used >= int(reward.max_redemptions_per_user):
            raise ValueError("سقف بازخرید شما برای این جایزه پر شده")

    cost = int(reward.points_cost)
    if cost <= 0:
        raise ValueError("هزینه جایزه نامعتبر است")
    if await points_balance(session, user, user_rid) < cost:
        raise ValueError("امتیاز کافی نیست")

    rtype = reward.reward_type
    rval = int(reward.reward_value)
    service: UserService | None = None
    wallet_reason: str | None = None

    if rtype in ("traffic_gb", "time_days"):
        if not service_id:
            raise ValueError("یک سرویس فعال انتخاب کنید")
        service = await session.get(UserService, int(service_id))
        if not await customer_may_use_service(session, service, int(user.id), user_rid):
            raise ValueError("سرویس معتبر نیست")
        if not service.pg_user_id:
            raise ValueError("سرویس به پنل متصل نیست")
    elif rtype == "wallet_credit":
        if rval <= 0:
            raise ValueError("مقدار اعتبار نامعتبر است")
    elif rtype == "discount_percent":
        if rval <= 0 or rval > 100:
            raise ValueError("درصد تخفیف نامعتبر است")
    else:
        raise ValueError("نوع جایزه پشتیبانی نمی‌شود")

    # Debit points first (no commit) so failed apply rolls back the charge
    pts_tx = await debit_points(
        session,
        user,
        cost,
        tx_type="redeem",
        source=f"reward:{reward.id}",
        reference=str(reward.id),
        description=f"بازخرید: {reward.name}",
        idempotency_key=f"pts:{key}",
        meta={"reward_id": reward.id, "reward_type": rtype, "reward_value": rval},
        commit=False,
        shop_id=user_rid,
    )

    try:
        if rtype in ("traffic_gb", "time_days"):
            await apply_service_reward(session, user, service, rtype, rval)
        elif rtype == "wallet_credit":
            wallet_reason = f"loyalty_reward:{reward.id}:{key}"
            from app.services.wallet import credit_wallet

            # Shop rewards credit the shop purse; platform rewards → main purse.
            await credit_wallet(
                session,
                user,
                rval,
                wallet_reason,
                shop_id=int(reward.reseller_id) if reward.reseller_id is not None else None,
                commit=False,
            )
        reward.redemption_count = int(reward.redemption_count or 0) + 1
        meta: dict[str, Any] = {"reward_name": reward.name}
        red = RewardRedemption(
            user_id=user.id,
            reward_id=reward.id,
            points_spent=cost,
            reward_type=rtype,
            reward_value=rval,
            status="completed",
            service_id=service.id if service else None,
            points_tx_id=pts_tx.id if pts_tx else None,
            wallet_reason=wallet_reason,
            idempotency_key=key,
            meta_json=json.dumps(meta, ensure_ascii=False),
        )
        session.add(red)
        await session.flush()

        if rtype == "discount_percent":
            ent = await _create_discount_entitlement(session, user, reward, red)
            red.discount_code = ent.code
            meta["discount_code"] = ent.code
            meta["expires_at"] = ent.expires_at.isoformat() if ent.expires_at else None
            red.meta_json = json.dumps(meta, ensure_ascii=False)

        await session.commit()
        await session.refresh(red)
        await session.refresh(user)
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(
                select(RewardRedemption).where(RewardRedemption.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        raise
    except Exception:
        await session.rollback()
        raise
    return red


async def _unique_discount_code(session: AsyncSession) -> str:
    import secrets
    import string

    alphabet = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = "LOY" + "".join(secrets.choice(alphabet) for _ in range(8))
        exists = (
            await session.execute(
                select(LoyaltyDiscountEntitlement.id).where(
                    LoyaltyDiscountEntitlement.code == code
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            return code
    raise ValueError("ساخت کد تخفیف ناموفق بود")


async def issue_discount_entitlement(
    session: AsyncSession,
    user: BotUser,
    *,
    percent: int,
    min_purchase_toman: int = 0,
    max_discount_toman: int | None = None,
    expires_days: int | None = None,
    redemption_id: int | None = None,
    wheel_spin_id: int | None = None,
    reseller_id: int | None = None,
) -> LoyaltyDiscountEntitlement:
    """Issue a personal one-time discount (reward redeem or lucky wheel)."""
    pct = int(percent)
    if pct <= 0 or pct > 100:
        raise ValueError("درصد تخفیف نامعتبر است")
    code = await _unique_discount_code(session)
    expires_at = None
    if expires_days is not None and int(expires_days) > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(expires_days))
    ent = LoyaltyDiscountEntitlement(
        user_id=int(user.id),
        reseller_id=int(reseller_id) if reseller_id is not None else None,
        redemption_id=int(redemption_id) if redemption_id is not None else None,
        wheel_spin_id=int(wheel_spin_id) if wheel_spin_id is not None else None,
        code=code,
        percent=pct,
        min_purchase_toman=max(0, int(min_purchase_toman or 0)),
        max_discount_toman=int(max_discount_toman)
        if max_discount_toman is not None
        else None,
        status="available",
        expires_at=expires_at,
    )
    session.add(ent)
    await session.flush()
    return ent


async def _create_discount_entitlement(
    session: AsyncSession,
    user: BotUser,
    reward: LoyaltyReward,
    redemption: RewardRedemption,
) -> LoyaltyDiscountEntitlement:
    return await issue_discount_entitlement(
        session,
        user,
        percent=int(reward.reward_value),
        min_purchase_toman=int(reward.min_purchase_toman or 0),
        max_discount_toman=int(reward.max_discount_toman)
        if reward.max_discount_toman is not None
        else None,
        expires_days=int(reward.expires_days)
        if reward.expires_days is not None
        else None,
        redemption_id=int(redemption.id),
        reseller_id=int(reward.reseller_id) if reward.reseller_id is not None else None,
    )


def _entitlement_expired(ent: LoyaltyDiscountEntitlement, *, now: datetime | None = None) -> bool:
    if not ent.expires_at:
        return False
    now = now or datetime.now(timezone.utc)
    exp = ent.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < now


async def list_available_discounts(
    session: AsyncSession, user_id: int, *, shop_id: int | None
) -> list[LoyaltyDiscountEntitlement]:
    """Discount codes the customer holds from one shop (None = platform)."""
    scope = (
        LoyaltyDiscountEntitlement.reseller_id == int(shop_id)
        if shop_id else LoyaltyDiscountEntitlement.reseller_id.is_(None)
    )
    rows = list(
        (
            await session.execute(
                select(LoyaltyDiscountEntitlement)
                .where(
                    LoyaltyDiscountEntitlement.user_id == int(user_id),
                    LoyaltyDiscountEntitlement.status == "available",
                    scope,
                )
                .order_by(LoyaltyDiscountEntitlement.id.desc())
            )
        ).scalars().all()
    )
    return [r for r in rows if not _entitlement_expired(r)]


def compute_loyalty_discount_amount(ent: LoyaltyDiscountEntitlement, base_amount: int) -> int:
    base = max(0, int(base_amount))
    if int(ent.min_purchase_toman or 0) > 0 and base < int(ent.min_purchase_toman):
        return 0
    pct = max(0, min(100, int(ent.percent or 0)))
    discount = int(base * pct // 100)
    if ent.max_discount_toman is not None:
        discount = min(discount, int(ent.max_discount_toman))
    return max(0, min(discount, base))


async def reserve_loyalty_discount(
    session: AsyncSession,
    *,
    user_id: int,
    code: str,
    order: Order,
    base_amount: int,
) -> tuple[int, str]:
    """Atomically reserve a personal loyalty discount onto a pending order."""
    normalized = (code or "").upper().strip()
    if not normalized:
        raise ValueError("کد تخفیف نامعتبر است")
    try:
        ent = (
            await session.execute(
                select(LoyaltyDiscountEntitlement)
                .where(LoyaltyDiscountEntitlement.code == normalized)
                .with_for_update()
            )
        ).scalar_one_or_none()
    except Exception:
        ent = (
            await session.execute(
                select(LoyaltyDiscountEntitlement).where(
                    LoyaltyDiscountEntitlement.code == normalized
                )
            )
        ).scalar_one_or_none()
    if not ent or int(ent.user_id) != int(user_id):
        raise ValueError("کد تخفیف نامعتبر است")
    # Shop discounts must not apply to platform (or other-shop) orders.
    ent_rid = int(ent.reseller_id) if getattr(ent, "reseller_id", None) else None
    order_rid = int(order.reseller_id) if order.reseller_id else None
    if ent_rid != order_rid:
        raise ValueError("این تخفیف برای این فروشگاه نیست")
    if ent.status == "reserved" and ent.reserved_order_id == int(order.id):
        return int(ent.discount_amount or 0), ent.code
    if ent.status != "available":
        raise ValueError("این تخفیف قبلاً استفاده شده")
    if _entitlement_expired(ent):
        ent.status = "void"
        await session.flush()
        raise ValueError("مهلت این تخفیف تمام شده")
    discount = compute_loyalty_discount_amount(ent, base_amount)
    if discount <= 0:
        if int(ent.min_purchase_toman or 0) > base_amount:
            raise ValueError(
                f"حداقل مبلغ خرید برای این تخفیف {int(ent.min_purchase_toman)} تومان است"
            )
        raise ValueError("تخفیف قابل اعمال نیست")
    claim = await session.execute(
        update(LoyaltyDiscountEntitlement)
        .where(
            LoyaltyDiscountEntitlement.id == ent.id,
            LoyaltyDiscountEntitlement.status == "available",
            LoyaltyDiscountEntitlement.user_id == int(user_id),
        )
        .values(
            status="reserved",
            reserved_order_id=int(order.id),
            reserved_at=datetime.now(timezone.utc),
            original_amount=int(base_amount),
            discount_amount=int(discount),
            final_amount=max(0, int(base_amount) - int(discount)),
        )
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        raise ValueError("این تخفیف هم‌اکنون قابل استفاده نیست")
    return discount, ent.code


async def release_loyalty_discount_for_order(session: AsyncSession, order: Order) -> None:
    """Return reserved loyalty discount to available (failed/cancelled unpaid order)."""
    code = (order.discount_code or "").strip().upper()
    if not code.startswith("LOY"):
        return
    ent = (
        await session.execute(
            select(LoyaltyDiscountEntitlement).where(
                LoyaltyDiscountEntitlement.code == code,
                LoyaltyDiscountEntitlement.status == "reserved",
                LoyaltyDiscountEntitlement.reserved_order_id == int(order.id),
            )
        )
    ).scalar_one_or_none()
    if not ent:
        return
    if _entitlement_expired(ent):
        ent.status = "void"
    else:
        ent.status = "available"
        ent.reserved_order_id = None
        ent.reserved_at = None
        ent.original_amount = None
        ent.discount_amount = None
        ent.final_amount = None
    await session.flush()


async def consume_loyalty_discount_for_order(session: AsyncSession, order: Order) -> None:
    """Mark reserved loyalty discount consumed after successful payment/delivery."""
    from app.services.gift_codes import consume_gift_discount

    await consume_gift_discount(session, order)
    code = (order.discount_code or "").strip().upper()
    if not code.startswith("LOY"):
        return
    now = datetime.now(timezone.utc)
    await session.execute(
        update(LoyaltyDiscountEntitlement)
        .where(
            LoyaltyDiscountEntitlement.code == code,
            LoyaltyDiscountEntitlement.status == "reserved",
            LoyaltyDiscountEntitlement.reserved_order_id == int(order.id),
        )
        .values(
            status="consumed",
            consumed_order_id=int(order.id),
            consumed_at=now,
        )
        .execution_options(synchronize_session=False)
    )


async def void_loyalty_discount_on_refund(session: AsyncSession, order: Order) -> None:
    """Refunded orders must not revive a consumed loyalty discount."""
    code = (order.discount_code or "").strip().upper()
    if not code.startswith("LOY"):
        return
    await session.execute(
        update(LoyaltyDiscountEntitlement)
        .where(
            LoyaltyDiscountEntitlement.code == code,
            LoyaltyDiscountEntitlement.consumed_order_id == int(order.id),
        )
        .values(status="void")
        .execution_options(synchronize_session=False)
    )


async def apply_service_reward(
    session: AsyncSession,
    user: BotUser,
    service: UserService,
    reward_type: str,
    reward_value: int,
) -> None:
    """Apply traffic/time to Pasarguard user (additive). Shared by redeem + lucky wheel."""
    import time

    from app.services.service_cancellations import lock_service_mutation
    await lock_service_mutation(session, service.id)

    from app.services.pasarguard import get_pg, get_pg_for_reseller
    from app.services.users import current_shop_reseller_id

    service_shop = await service_shop_id(session, service)
    if service_shop != (current_shop_reseller_id() or None):
        raise ValueError("سرویس متعلق به این فروشگاه نیست")
    if service_shop:
        # Shop path: never fall back to Owner/platform PG credentials
        pg = await get_pg_for_reseller(session, int(service_shop))
    else:
        pg = get_pg()

    info = await pg.get_user_by_id(int(service.pg_user_id))
    payload: dict[str, Any] = {"status": "active"}
    if reward_type == "traffic_gb":
        add_bytes = int(reward_value) * (1024**3)
        current = info.get("data_limit")
        if current is None or int(current or 0) <= 0:
            raise ValueError("سرویس نامحدود است؛ جایزه ترافیک قابل اعمال نیست")
        payload["data_limit"] = int(current) + add_bytes
    elif reward_type == "time_days":
        add_sec = int(reward_value) * 86400
        expire = info.get("expire")
        # Pasarguard may return ISO or unix
        base = int(time.time())
        if expire:
            try:
                if isinstance(expire, (int, float)):
                    base = max(base, int(expire))
                else:
                    from datetime import datetime as dt

                    parsed = dt.fromisoformat(str(expire).replace("Z", "+00:00"))
                    base = max(base, int(parsed.timestamp()))
            except Exception:
                pass
        payload["expire"] = base + add_sec
    await pg.modify_user_by_id(int(service.pg_user_id), payload)


async def overview_metrics(
    session: AsyncSession, *, reseller_id: int | None = None
) -> dict[str, Any]:
    user_filter = [BotUser.is_demo.is_(False), non_demo_customer(BotUser.referred_by_id)]
    if reseller_id is not None:
        user_filter.append(BotUser.reseller_id == int(reseller_id))

    total_refs = int(
        (
            await session.execute(
                select(func.count())
                .select_from(BotUser)
                .where(BotUser.referred_by_id.is_not(None), *user_filter)
            )
        ).scalar_one()
        or 0
    )
    qual_q = select(func.count()).select_from(ReferralEvent).where(
        ReferralEvent.qualification_state == "qualified"
    )
    if reseller_id is not None:
        qual_q = (
            select(func.count())
            .select_from(ReferralEvent)
            .join(BotUser, BotUser.id == ReferralEvent.referrer_id)
            .where(
                ReferralEvent.qualification_state == "qualified",
                BotUser.reseller_id == int(reseller_id),
            )
        )
    qual_q = qual_q.where(
        non_demo_customer(ReferralEvent.referrer_id), non_demo_customer(ReferralEvent.referred_id),
    )
    qualified = int((await session.execute(qual_q)).scalar_one() or 0)

    # Points and redeemed credits belong to the shop whose purse or reward they are.
    points_scope = (
        PointsTransaction.reseller_id == int(reseller_id)
        if reseller_id is not None
        else PointsTransaction.reseller_id.is_(None)
    )
    points_sum = select(func.coalesce(func.sum(PointsTransaction.amount), 0)).where(
        points_scope, non_demo_customer(PointsTransaction.user_id)
    )
    issued = int((await session.execute(points_sum.where(PointsTransaction.amount > 0))).scalar_one() or 0)
    redeemed = int(
        (
            await session.execute(
                points_sum.where(PointsTransaction.amount < 0, PointsTransaction.tx_type == "redeem")
            )
        ).scalar_one()
        or 0
    )

    reward_scope = (
        LoyaltyReward.reseller_id == int(reseller_id)
        if reseller_id is not None
        else LoyaltyReward.reseller_id.is_(None)
    )
    red_q = (
        select(func.coalesce(func.sum(RewardRedemption.reward_value), 0))
        .select_from(RewardRedemption)
        .join(LoyaltyReward, LoyaltyReward.id == RewardRedemption.reward_id)
        .where(
            RewardRedemption.reward_type == "wallet_credit",
            RewardRedemption.status == "completed",
            reward_scope,
            non_demo_customer(RewardRedemption.user_id),
        )
    )
    wallet_credits = int((await session.execute(red_q)).scalar_one() or 0)

    rw_q = select(func.count()).select_from(LoyaltyReward).where(
        LoyaltyReward.enabled.is_(True), LoyaltyReward.archived.is_(False)
    )
    if reseller_id is None:
        rw_q = rw_q.where(LoyaltyReward.reseller_id.is_(None))
    else:
        rw_q = rw_q.where(LoyaltyReward.reseller_id == int(reseller_id))
    active_rewards = int((await session.execute(rw_q)).scalar_one() or 0)

    top_q = (
        select(BotUser.referred_by_id, func.count())
        .where(BotUser.referred_by_id.is_not(None), *user_filter)
        .group_by(BotUser.referred_by_id)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_rows = (await session.execute(top_q)).all()
    top: list[dict[str, Any]] = []
    for rid, cnt in top_rows:
        u = await session.get(BotUser, int(rid))
        top.append(
            {
                "user_id": int(rid),
                "label": bot_user_panel_label(u, fallback_id=int(rid)) if u else str(rid),
                "count": int(cnt),
            }
        )
    return {
        "total_referrals": total_refs,
        "qualified_referrals": qualified,
        "points_issued": issued,
        "points_redeemed": abs(redeemed),
        "wallet_credits_issued": wallet_credits,
        "active_rewards": active_rewards,
        "top_referrers": top,
    }
