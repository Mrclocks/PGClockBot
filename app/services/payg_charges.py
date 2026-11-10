"""The PAYG charge ledger: charge a shop before a panel change, then settle or refund it.

Order of a charged change:

1. ``reserve_charge`` prices the change from the shop's package, writes a ``pending``
   ``ResellerCharge`` and debits the shop wallet in one commit, before any panel call.
2. The caller makes the panel call.
3. ``settle_charge`` on success; ``refund_charge`` only when the panel definitely
   refused; ``send_to_review`` when the outcome is unknown. A reviewed charge keeps
   ``panel_target`` so a retry repeats exactly the same change.

Every transition is an atomic status claim, so a repeated or concurrent call moves the
money once. The request key comes from the caller (one per confirmed change, for example
``order:<id>``) and is scoped to the shop. A retry with the same key reuses an open or
settled charge; only after a refund does it charge again, as the next ``attempt``.
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

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChildPricePlan, ResellerCharge, ResellerPlan, ResellerProfile
from app.py_compat import StrEnum
from app.services.billing import (
    KIND_CHARGE,
    KIND_CHARGE_REFUND,
    is_payg,
    record_payg_ledger_entry,
)
from app.services.child_pricing import parent_shop_profile
from app.services.payg_pricing import (
    ChargeQuote,
    PaygRates,
    ServiceChange,
    assert_quote_unchanged,
    child_plan_id_of,
    quote_change,
    rates_from_plan,
)

logger = logging.getLogger(__name__)

REVIEW_PAGE_LIMIT = 50
_REQUEST_KEY = re.compile(r"[A-Za-z0-9_:-]{8,120}")


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
    quantity: int = 1
    expected_amount: int | None = None
    created_by: str | None = None


def new_request_key() -> str:
    return secrets.token_hex(16)


def checked_request_key(request_key: str) -> str:
    key = str(request_key or "")
    if not _REQUEST_KEY.fullmatch(key):
        raise ValueError("کلید یکتای عملیات نامعتبر است")
    return key


def _idempotency_key(shop_id: int, request_key: str, attempt: int) -> str:
    return f"payg:{int(shop_id)}:{request_key}#{int(attempt)}"


async def shop_rates(session: AsyncSession, profile: ResellerProfile) -> PaygRates | None:
    """The shop's PAYG rates, or ``None`` for a fixed-package shop."""
    if not is_payg(profile):
        return None
    child_plan_id = child_plan_id_of(profile)
    if child_plan_id is not None:
        # A level-2 shop priced by its parent: the parent's list, always charged at creation.
        child_plan = await session.get(ChildPricePlan, child_plan_id)
        return rates_from_plan(child_plan) if child_plan is not None else None
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


async def _latest_attempt(
    session: AsyncSession, shop_id: int, request_key: str
) -> ResellerCharge | None:
    return await session.scalar(
        select(ResellerCharge)
        .where(ResellerCharge.shop_id == int(shop_id), ResellerCharge.request_key == request_key)
        .order_by(ResellerCharge.attempt.desc())
        .limit(1)
    )


def _reusable(charge: ResellerCharge | None) -> ResellerCharge | None:
    """An earlier attempt that still holds the money (or was settled) is the answer."""
    if charge is not None and charge.status != ChargeStatus.REFUNDED:
        return charge
    return None


def _new_charge(
    profile: ResellerProfile, request: ChargeRequest, quote: ChargeQuote, *, request_key: str, attempt: int
) -> ResellerCharge:
    shop_id = int(profile.user_id)
    return ResellerCharge(
        shop_id=shop_id,
        plan_id=int(profile.plan_id) if profile.plan_id else None,
        event=str(request.change.event),
        status=ChargeStatus.PENDING,
        request_key=request_key,
        attempt=attempt,
        quantity=int(request.quantity),
        pg_username=(request.pg_username or "")[:128] or None,
        bytes_charged=quote.bytes_charged,
        days_charged=quote.days_charged,
        user_units=quote.user_units,
        price_per_gb=quote.price_per_gb,
        user_price=quote.user_price,
        is_unlimited=quote.is_unlimited,
        amount=quote.amount,
        idempotency_key=_idempotency_key(shop_id, request_key, attempt),
        panel_target=json.dumps(dict(request.panel_target), sort_keys=True, ensure_ascii=False),
        created_by=(request.created_by or "")[:128] or None,
    )


async def reserve_charge(
    session: AsyncSession, profile: ResellerProfile, request: ChargeRequest
) -> ResellerCharge | None:
    """Price the change and take the money before the panel call.

    Returns ``None`` when the change costs nothing. The same request key returns the
    charge it already made unless that one was refunded. Raises ``ValueError`` (safe to
    show) on a shortfall, a price that changed since it was shown, or a change the package
    may not sell.
    """
    shop_id = int(profile.user_id)
    request_key = checked_request_key(request.request_key)
    latest = await _latest_attempt(session, shop_id, request_key)
    if _reusable(latest) is not None:
        return latest
    unit_quote = await quote_for_shop(session, profile, request.change)
    if unit_quote is None or unit_quote.is_free:
        return None
    quote = unit_quote.for_quantity(request.quantity)
    if request.expected_amount is not None:
        assert_quote_unchanged(request.expected_amount, quote)
    attempt = int(latest.attempt) + 1 if latest is not None else 1
    charge = _new_charge(profile, request, quote, request_key=request_key, attempt=attempt)
    parent = await _parent_charge(session, profile, request, request_key=request_key, attempt=attempt)
    try:
        # A savepoint keeps a refusal (shortfall, racing key) from rolling back the
        # caller's session and expiring the order or service it is working on.
        async with session.begin_nested():
            session.add(charge)
            await session.flush()
            await _debit(session, profile, charge, request)
            if parent is not None:
                parent_profile, parent_charge = parent
                session.add(parent_charge)
                await session.flush()
                charge.linked_charge_id = int(parent_charge.id)
                await _debit(session, parent_profile, parent_charge, request)
    except IntegrityError:
        winner = _reusable(await _latest_attempt(session, shop_id, request_key))
        if winner is None:
            raise
        return winner
    except ValueError:
        # The savepoint rollback expired the profile; reload it for the caller's alert.
        await session.refresh(profile)
        raise
    await session.commit()
    return charge


async def _debit(session: AsyncSession, profile: ResellerProfile, charge: ResellerCharge, request: ChargeRequest) -> None:
    await record_payg_ledger_entry(
        session,
        profile,
        kind=KIND_CHARGE,
        amount=-int(charge.amount),
        idempotency_key=charge.idempotency_key,
        note=f"{request.change.event} {request.pg_username}"[:255],
        created_by=request.created_by,
        use_credit=True,
    )


async def _parent_charge(
    session: AsyncSession, profile: ResellerProfile, request: ChargeRequest, *, request_key: str, attempt: int
) -> tuple[ResellerProfile, ResellerCharge] | None:
    """The level-1 parent's charge (owner's price) for a change its level-2 shop is charged for."""
    if child_plan_id_of(profile) is None:
        return None
    parent_profile = await parent_shop_profile(session, profile)
    if parent_profile is None:
        return None
    rates = await shop_rates(session, parent_profile)
    if rates is None or not (rates.charges_traffic or rates.charges_users):
        return None
    quote = quote_change(rates, request.change).for_quantity(request.quantity)
    if quote.is_free:
        return None
    return parent_profile, _new_charge(parent_profile, request, quote, request_key=request_key, attempt=attempt)


async def _with_linked(session: AsyncSession, charge: ResellerCharge) -> list[ResellerCharge]:
    """A level-2 charge and the parent charge it moves with."""
    linked = await session.get(ResellerCharge, int(charge.linked_charge_id)) if charge.linked_charge_id else None
    return [charge] + ([linked] if linked is not None else [])


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


async def settle_charge(session: AsyncSession, charge: ResellerCharge, *, commit: bool = True) -> bool:
    """The panel applied the change. False when the charge was already resolved.

    ``commit=False`` joins the caller's transaction (for example the one that seals the order).
    """
    charges = await _with_linked(session, charge)
    claimed = await _claim(session, charges[0], allowed=_OPEN, to=ChargeStatus.SETTLED)
    for linked in charges[1:]:
        await _claim(session, linked, allowed=_OPEN, to=ChargeStatus.SETTLED)
    if commit:
        await session.commit()
    return claimed


async def send_to_review(session: AsyncSession, charge: ResellerCharge) -> bool:
    """The panel outcome is unknown: keep the money and the target for a retry."""
    charges = await _with_linked(session, charge)
    claimed = await _claim(session, charges[0], allowed=(ChargeStatus.PENDING,), to=ChargeStatus.REVIEW)
    for linked in charges[1:]:
        await _claim(session, linked, allowed=(ChargeStatus.PENDING,), to=ChargeStatus.REVIEW)
    await session.commit()
    return claimed


async def refund_charge(session: AsyncSession, charge: ResellerCharge, *, reason: str) -> bool:
    """The panel definitely did not apply the change: return the money once (to the parent too)."""
    charges = await _with_linked(session, charge)
    if not await _claim(session, charges[0], allowed=_OPEN, to=ChargeStatus.REFUNDED):
        # Already settled or refunded: the claim wrote nothing, so the caller's session is untouched.
        return False
    for item in charges:
        if item is not charges[0] and not await _claim(session, item, allowed=_OPEN, to=ChargeStatus.REFUNDED):
            continue
        profile = await session.scalar(
            select(ResellerProfile).where(ResellerProfile.user_id == int(item.shop_id))
        )
        if profile is None:
            await session.rollback()
            raise ValueError("نماینده یافت نشد")
        await record_payg_ledger_entry(
            session,
            profile,
            kind=KIND_CHARGE_REFUND,
            amount=int(item.amount),
            idempotency_key=f"payg-refund:{int(item.id)}",
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
    # A parent's half of a two-step charge moves with its level-2 charge, so only that one is listed.
    linked_parents = select(ResellerCharge.linked_charge_id).where(ResellerCharge.linked_charge_id.is_not(None))
    query = select(ResellerCharge).where(
        ResellerCharge.status == str(ChargeStatus.REVIEW), ResellerCharge.id.not_in(linked_parents)
    )
    if shop_id is not None:
        query = query.where(ResellerCharge.shop_id == int(shop_id))
    bounded = max(1, min(int(limit), REVIEW_PAGE_LIMIT))
    query = query.order_by(ResellerCharge.id.desc()).limit(bounded)
    return list((await session.scalars(query)).all())


async def count_review_charges(session: AsyncSession) -> int:
    """How many charges wait for an owner decision (all shops)."""
    return int(
        await session.scalar(
            select(func.count()).select_from(ResellerCharge).where(
                ResellerCharge.status == str(ChargeStatus.REVIEW),
                ResellerCharge.id.not_in(
                    select(ResellerCharge.linked_charge_id).where(ResellerCharge.linked_charge_id.is_not(None))
                ),
            )
        )
        or 0
    )


async def resolve_review_charge(session: AsyncSession, charge_id: int, *, applied: bool, actor: str) -> bool:
    """Owner decision on a charge in review: the change applied (settle) or not (refund).

    False when the charge is not in review any more (already decided, or never was).
    """
    charge = await session.get(ResellerCharge, int(charge_id))
    if charge is None or charge.status != ChargeStatus.REVIEW:
        return False
    if applied:
        return await settle_charge(session, charge)
    return await refund_charge(session, charge, reason=f"بازبینی: تغییر اعمال نشده بود ({actor})"[:255])
