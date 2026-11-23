"""How much of a service's payment a cancellation gives back.

The base is what the customer paid for the service's current period: the last delivered purchase
or renewal, plus the add-on packs delivered after it. Only orders the service's current owner paid
count. In «remaining» mode the base is scaled by the smaller of the time share and the volume share
still left on the panel, so a customer who used most of either cannot reclaim the rest. The fee is
taken last. All arithmetic stays in whole currency units; a fraction is always rounded down.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, UserService
from app.services.formatting import parse_expire
from app.services.service_cancellation_settings import REFUND_FULL, REFUND_NONE, CancellationPolicy
from app.services.service_period import period_usage

# A service has a handful of orders; the newest ones always contain the current period.
PERIOD_ORDER_LIMIT = 50
PURCHASE_REMARK_PREFIX = "order:"
FULL_SHARE = Fraction(1)


@dataclass(frozen=True, slots=True)
class PeriodPayment:
    amount: int
    # When the current period was bought; None when no paid order of this owner is found.
    started_at: datetime | None


@dataclass(frozen=True, slots=True)
class RefundQuote:
    paid_amount: int
    remaining_percent: int
    amount: int


def _purchase_order_id(service: UserService) -> int | None:
    remark = str(service.remark or "")
    if not remark.startswith(PURCHASE_REMARK_PREFIX):
        return None
    raw = remark[len(PURCHASE_REMARK_PREFIX):]
    return int(raw) if raw.isdigit() else None


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


async def period_payment(session: AsyncSession, service: UserService) -> PeriodPayment:
    """What the owner paid for the current period, newest orders first.

    A purchase or a renewal carries a plan and opens a period; an add-on pack has no plan and adds
    to the period it was bought in. A purchase of several services is shared between them.
    """
    purchase_id = _purchase_order_id(service)
    linked = Order.service_id == int(service.id)
    if purchase_id is not None:
        linked = or_(linked, Order.id == purchase_id)
    rows = (await session.execute(
        select(Order.plan_id, Order.amount, Order.quantity, Order.delivered_at, Order.created_at)
        .where(linked, Order.user_id == int(service.bot_user_id), Order.status == OrderStatus.DELIVERED.value)
        .order_by(Order.id.desc())
        .limit(PERIOD_ORDER_LIMIT)
    )).all()
    total = 0
    for plan_id, amount, quantity, delivered_at, created_at in rows:
        share = max(int(amount or 0), 0) // max(int(quantity or 1), 1)
        total += share
        if plan_id is not None:
            return PeriodPayment(total, _utc(delivered_at or created_at))
    return PeriodPayment(total, None)


def _time_share(info: Mapping[str, Any], started_at: datetime | None, now: datetime) -> Fraction:
    expire = parse_expire(info.get("expire") if "expire" in info else info.get("expire_date"))
    if expire is None:
        # Unlimited time, or an on-hold service whose clock has not started.
        return FULL_SHARE
    if expire <= now:
        return Fraction(0)
    if started_at is None or started_at >= expire:
        return FULL_SHARE
    total = (expire - started_at).total_seconds()
    left = (expire - now).total_seconds()
    return min(Fraction(int(left), max(int(total), 1)), FULL_SHARE)


def _volume_share(info: Mapping[str, Any], service: UserService) -> Fraction:
    try:
        limit = int(info.get("data_limit") or 0)
        used = int(info.get("used_traffic") or 0)
    except (TypeError, ValueError):
        return Fraction(0)
    if limit <= 0:
        return FULL_SHARE
    used, limit = period_usage(service, used, limit, info.get("lifetime_used_traffic"))
    if limit <= 0 or used >= limit:
        return Fraction(0)
    return Fraction(int(limit - used), int(limit))


def remaining_share(info: Mapping[str, Any], service: UserService, started_at: datetime | None, now: datetime) -> Fraction:
    """The smaller of the time and the volume still left, between 0 and 1."""
    return max(min(_time_share(info, started_at, now), _volume_share(info, service)), Fraction(0))


def quote_refund(policy: CancellationPolicy, payment: PeriodPayment, share: Fraction) -> RefundQuote:
    percent = int(share * 100)
    if policy.refund == REFUND_NONE:
        gross = 0
    elif policy.refund == REFUND_FULL:
        gross = payment.amount
    else:
        gross = int(payment.amount * share)
    amount = gross * (100 - policy.fee_percent) // 100
    return RefundQuote(paid_amount=payment.amount, remaining_percent=percent, amount=max(amount, 0))


def outside_window(policy: CancellationPolicy, payment: PeriodPayment, service: UserService, now: datetime) -> bool:
    """True when the shop's cancellation window since the purchase or renewal has passed."""
    if policy.window_days <= 0:
        return False
    started = payment.started_at or _utc(service.created_at)
    return started is not None and now - started > timedelta(days=policy.window_days)
