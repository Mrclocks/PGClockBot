"""The PAYG charge ledger: charge a shop before a panel change, then settle or refund it.

Order of a charged change:

1. ``reserve_charge`` prices the change from the shop's package, writes a ``pending``
   ``ResellerCharge`` and debits the shop wallet in one commit, before any panel call.
2. The caller makes the panel call.
3. ``settle_charge`` on success; ``refund_charge`` only when the panel definitely
   refused; ``send_to_review`` when the outcome is unknown. A reviewed charge keeps
   ``panel_target`` so a retry repeats exactly the same change.

Every transition is an atomic status claim, so a repeated or concurrent call moves the
money once. The request key comes from the caller (one per confirmed change) and is
scoped to the shop.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerCharge, ResellerPlan, ResellerProfile
from app.py_compat import StrEnum
from app.services.billing import (
    KIND_CHARGE,
    KIND_CHARGE_REFUND,
    is_payg,
    record_payg_ledger_entry,
)
from app.services.payg_pricing import (
    ChargeQuote,
    PaygRates,
    ServiceChange,
    assert_quote_unchanged,
    quote_change,
    rates_from_plan,
)

logger = logging.getLogger(__name__)

REVIEW_PAGE_LIMIT = 50
_REQUEST_KEY = re.compile(r"[A-Za-z0-9_-]{8,64}")


class ChargeStatus(StrEnum):
    PENDING = "pending"
    SETTLED = "settled"
    REFUNDED = "refunded"
    REVIEW = "review"


_OPEN = (ChargeStatus.PENDING, ChargeStatus.REVIEW)


@dataclass(frozen=True, slots=True)
class ChargeRequest:
    """One confirmed change. ``panel_target`` is the exact panel payload to repeat on retry."""

    request_key: str
    change: ServiceChange
    pg_username: str
    panel_target: Mapping[str, Any] = field(default_factory=dict)
    expected_amount: int | None = None
    created_by: str | None = None


def new_request_key() -> str:
    return secrets.token_hex(16)


def charge_key(shop_id: int, request_key: str) -> str:
    key = str(request_key or "")
    if not _REQUEST_KEY.fullmatch(key):
        raise ValueError("کلید یکتای عملیات نامعتبر است")
    return f"payg:{int(shop_id)}:{key}"


async def shop_rates(session: AsyncSession, profile: ResellerProfile) -> PaygRates | None:
    """The shop's PAYG rates, or ``None`` for a fixed-package shop."""
    if not is_payg(profile):
        return None
    plan = await session.get(ResellerPlan, int(profile.plan_id)) if profile.plan_id else None
    return rates_from_plan(plan)


async def quote_for_shop(
    session: AsyncSession, profile: ResellerProfile, change: ServiceChange
) -> ChargeQuote | None:
    """What ``change`` costs this shop now; ``None`` when nothing is charged at creation."""
    rates = await shop_rates(session, profile)
    if rates is None or not (rates.charges_traffic or rates.charges_users):
        return None
    return quote_change(rates, change)


async def _charge_by_key(session: AsyncSession, key: str) -> ResellerCharge | None:
    return await session.scalar(select(ResellerCharge).where(ResellerCharge.idempotency_key == key))


def _own(charge: ResellerCharge, shop_id: int) -> ResellerCharge:
    if int(charge.shop_id) != int(shop_id):
        raise ValueError("کلید یکتای عملیات نامعتبر است")
    return charge


def _new_charge(shop_id: int, profile: ResellerProfile, request: ChargeRequest, quote: ChargeQuote, key: str) -> ResellerCharge:
    return ResellerCharge(
        shop_id=shop_id,
        plan_id=int(profile.plan_id) if profile.plan_id else None,
        event=str(request.change.event),
        status=ChargeStatus.PENDING,
        pg_username=(request.pg_username or "")[:128] or None,
        bytes_charged=quote.bytes_charged,
        days_charged=quote.days_charged,
        user_units=quote.user_units,
        price_per_gb=quote.price_per_gb,
        user_price=quote.user_price,
        is_unlimited=quote.is_unlimited,
        amount=quote.amount,
        idempotency_key=key,
        panel_target=json.dumps(dict(request.panel_target), sort_keys=True, ensure_ascii=False),
        created_by=(request.created_by or "")[:128] or None,
    )


async def reserve_charge(
    session: AsyncSession, profile: ResellerProfile, request: ChargeRequest
) -> ResellerCharge | None:
    """Price the change and take the money before the panel call.

    Returns ``None`` when the change costs nothing. The same request key returns the
    charge it already made. Raises ``ValueError`` (safe to show) on a shortfall, a price
    that changed since it was shown, or a change the package may not sell.
    """
    shop_id = int(profile.user_id)
    key = charge_key(shop_id, request.request_key)
    existing = await _charge_by_key(session, key)
    if existing is not None:
        return _own(existing, shop_id)
    quote = await quote_for_shop(session, profile, request.change)
    if quote is None or quote.is_free:
        return None
    if request.expected_amount is not None:
        assert_quote_unchanged(request.expected_amount, quote)
    charge = _new_charge(shop_id, profile, request, quote, key)
    try:
        session.add(charge)
        await session.flush()
        await record_payg_ledger_entry(
            session,
            profile,
            kind=KIND_CHARGE,
            amount=-quote.amount,
            idempotency_key=key,
            note=f"{request.change.event} {request.pg_username}"[:255],
            created_by=request.created_by,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        winner = await _charge_by_key(session, key)
        if winner is None:
            raise
        return _own(winner, shop_id)
    except ValueError:
        await session.rollback()
        raise
    return charge


async def _claim(
    session: AsyncSession, charge: ResellerCharge, *, allowed: tuple[str, ...], to: ChargeStatus
) -> bool:
    resolved = datetime.now(timezone.utc) if to in (ChargeStatus.SETTLED, ChargeStatus.REFUNDED) else None
    result = await session.execute(
        update(ResellerCharge)
        .where(
            ResellerCharge.id == int(charge.id),
            ResellerCharge.shop_id == int(charge.shop_id),
            ResellerCharge.status.in_([str(s) for s in allowed]),
        )
        .values(status=str(to), resolved_at=resolved)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def settle_charge(session: AsyncSession, charge: ResellerCharge) -> bool:
    """The panel applied the change. False when the charge was already resolved."""
    claimed = await _claim(session, charge, allowed=_OPEN, to=ChargeStatus.SETTLED)
    await session.commit()
    return claimed


async def send_to_review(session: AsyncSession, charge: ResellerCharge) -> bool:
    """The panel outcome is unknown: keep the money and the target for a retry."""
    claimed = await _claim(session, charge, allowed=(ChargeStatus.PENDING,), to=ChargeStatus.REVIEW)
    await session.commit()
    return claimed


async def refund_charge(session: AsyncSession, charge: ResellerCharge, *, reason: str) -> bool:
    """The panel definitely did not apply the change: return the money once."""
    if not await _claim(session, charge, allowed=_OPEN, to=ChargeStatus.REFUNDED):
        await session.rollback()
        return False
    profile = await session.scalar(
        select(ResellerProfile).where(ResellerProfile.user_id == int(charge.shop_id))
    )
    if profile is None:
        await session.rollback()
        raise ValueError("نماینده یافت نشد")
    await record_payg_ledger_entry(
        session,
        profile,
        kind=KIND_CHARGE_REFUND,
        amount=int(charge.amount),
        idempotency_key=f"payg-refund:{int(charge.id)}",
        note=(reason or "")[:255],
    )
    await session.commit()
    return True


def panel_target_of(charge: ResellerCharge) -> dict[str, Any]:
    try:
        data = json.loads(charge.panel_target or "{}")
    except (TypeError, ValueError):
        logger.warning("unreadable panel target charge=%s", charge.id)
        return {}
    return data if isinstance(data, dict) else {}


async def list_review_charges(
    session: AsyncSession, *, shop_id: int | None, limit: int = REVIEW_PAGE_LIMIT
) -> list[ResellerCharge]:
    """Charges waiting for review, newest first; ``shop_id=None`` is the owner's view of all shops."""
    query = select(ResellerCharge).where(ResellerCharge.status == str(ChargeStatus.REVIEW))
    if shop_id is not None:
        query = query.where(ResellerCharge.shop_id == int(shop_id))
    bounded = max(1, min(int(limit), REVIEW_PAGE_LIMIT))
    query = query.order_by(ResellerCharge.id.desc()).limit(bounded)
    return list((await session.scalars(query)).all())
