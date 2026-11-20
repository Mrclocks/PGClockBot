"""Cashback on wallet top-ups: a tiered bonus from the shop's own cashback rules.

A shop gives a bonus for charging the wallet with rules whose event is ``topup``. Tiers are
rules with different minimum amounts: in the default «best rule» mode the biggest one that
applies wins (for example 3% from 500,000 and 6% from 1,000,000 toman). The bonus is stored as
one pending ``CashbackGrant`` keyed by the payment (partial unique index), after the top-up
credit is committed, so a repeated approval never pays twice; ``cashback.mature_grants`` credits
it to the same shop's wallet once the hold passes. Voiding the top-up cancels a pending bonus or
takes a credited one back.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CashbackGrant, Payment, PaymentStatus
from app.services.cashback import (
    DEFAULT_HOLD_HOURS,
    EVENT_TOPUP,
    MAX_HOLD_HOURS,
    GrantStatus,
    Purchase,
    active_rules,
    cashback_enabled,
    claim_budget,
    combine,
    int_setting,
    reverse_grant,
    rule_amount,
    shop_setting,
    within_customer_caps,
)
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)


def _payment_shop(payment: Payment) -> int | None:
    return int(payment.wallet_shop_id) if payment.wallet_shop_id else None


async def _first_topup(session: AsyncSession, payment: Payment, shop_id: int | None) -> bool:
    shop = Payment.wallet_shop_id.is_(None) if shop_id is None else Payment.wallet_shop_id == shop_id
    earlier = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.user_id == int(payment.user_id), Payment.is_wallet_topup.is_(True), shop,
            Payment.status == PaymentStatus.APPROVED.value, Payment.id < int(payment.id),
        )
    )
    return not earlier


async def quote_topup_bonus(session: AsyncSession, payment: Payment, *, at: datetime) -> tuple[int | None, int]:
    """(rule id, amount) the top-up earns now; (None, 0) when none."""
    shop_id = _payment_shop(payment)
    amount = int(payment.amount or 0)
    if amount <= 0 or not await cashback_enabled(session, shop_id):
        return None, 0
    purchase = Purchase(
        amount=amount, event=EVENT_TOPUP, plan_id=None, payment_method=str(payment.method or ""),
        has_discount=False, first_purchase=await _first_topup(session, payment, shop_id), at=at,
    )
    amounts = []
    for rule in await active_rules(session, shop_id):
        bonus = rule_amount(rule, purchase)
        if bonus and await within_customer_caps(
            session, rule, user_id=int(payment.user_id), shop_id=shop_id, amount=bonus, at=at
        ):
            amounts.append((rule, bonus))
    mode = await shop_setting(session, "cashback_combine", "best", shop_id)
    return combine(amounts, mode=mode, price=amount)


async def _existing(session: AsyncSession, payment_id: int) -> CashbackGrant | None:
    return await session.scalar(select(CashbackGrant).where(CashbackGrant.payment_id == payment_id))


async def grant_for_topup(session: AsyncSession, payment: Payment, *, now: datetime | None = None) -> CashbackGrant | None:
    """Store this approved top-up's bonus once; ``None`` when it earns none."""
    now = now or datetime.now(timezone.utc)
    if not payment.is_wallet_topup or payment.status != PaymentStatus.APPROVED.value:
        return None
    # Captured first: a rollback below expires ``payment``.
    payment_id, shop_id, user_id, base = int(payment.id), _payment_shop(payment), int(payment.user_id), int(payment.amount)
    existing = await _existing(session, payment_id)
    if existing is not None:
        return existing
    rule_id, amount = await quote_topup_bonus(session, payment, at=now)
    if amount <= 0:
        return None
    if not await claim_budget(session, shop_id, amount, at=now):
        await session.rollback()
        logger.info("cashback budget exhausted shop=%s topup=%s", shop_id, payment_id)
        return None
    hours = await int_setting(session, "cashback_hold_hours", DEFAULT_HOLD_HOURS, shop_id, upper=MAX_HOLD_HOURS)
    grant = CashbackGrant(
        shop_id=shop_id, user_id=user_id, order_id=None, payment_id=payment_id, rule_id=rule_id,
        base_amount=base, amount=amount, status=str(GrantStatus.PENDING), available_at=now + timedelta(hours=hours),
    )
    try:
        async with session.begin_nested():
            session.add(grant)
            await session.flush()
    except IntegrityError:
        # A concurrent approval granted this top-up; undo this run's budget claim with it.
        await session.rollback()
        return await _existing(session, payment_id)
    await session.commit()
    return grant


async def grant_after_topup(session: AsyncSession, payment_id: int) -> None:
    """Best effort after a committed top-up credit: a bonus failure never undoes the credit."""
    try:
        # Reloaded: the caller's commit may have expired the payment it holds.
        payment = await session.get(Payment, int(payment_id), populate_existing=True)
        if payment is not None:
            await grant_for_topup(session, payment)
    except Exception:  # noqa: BLE001 - optional reward after a committed credit; logged
        logger.warning("top-up cashback grant failed payment=%s", payment_id, exc_info=True)
        await rollback_quiet(session)


async def reverse_after_topup_void(session: AsyncSession, payment_id: int, *, reason: str) -> None:
    """Best effort after a committed top-up void; logged, never raised."""
    try:
        grant = await _existing(session, int(payment_id))
        if grant is not None:
            await reverse_grant(session, grant, reason=reason, now=datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - the void already committed; a failed reversal is logged
        logger.warning("top-up cashback reversal failed payment=%s", payment_id, exc_info=True)
        await rollback_quiet(session)
