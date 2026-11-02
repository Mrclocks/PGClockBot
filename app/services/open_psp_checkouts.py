"""Pending gateway payments whose checkout link the customer may still pay.

A PSP gateway takes the money before PGClockBot verifies it, so a pending payment
with an open PSP settlement must stay payable: rejecting it when the customer picks
a payment method again would turn a later successful payment into money with no order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, PaymentMethod, PaymentSettlement, SettlementStatus

# Same value as payment_settlement.CHANNEL_PSP; that module imports orders, so it
# cannot be imported here without a cycle.
PSP_SETTLEMENT_CHANNEL = "psp"
# A re-tap within this window shows the same gateway link instead of minting a new
# one; older links may have expired at the gateway, so a fresh one is created.
PSP_CHECKOUT_REUSE_WINDOW = timedelta(minutes=10)
_OPEN_STATUSES = (
    SettlementStatus.CREATED.value,
    SettlementStatus.AWAITING.value,
    SettlementStatus.SETTLING.value,
)


@dataclass(slots=True, frozen=True)
class OpenPspCheckout:
    payment_id: int
    status: str
    has_link: bool
    created_at: datetime | None


async def open_psp_checkouts(
    session: AsyncSession, payment_ids: list[int]
) -> dict[int, OpenPspCheckout]:
    """Open PSP settlements of the given payments, keyed by payment id."""
    if not payment_ids:
        return {}
    rows = await session.execute(
        select(
            PaymentSettlement.payment_id,
            PaymentSettlement.status,
            PaymentSettlement.checkout_url,
            PaymentSettlement.created_at,
        ).where(
            PaymentSettlement.payment_id.in_(payment_ids),
            PaymentSettlement.channel == PSP_SETTLEMENT_CHANNEL,
            PaymentSettlement.status.in_(_OPEN_STATUSES),
        )
    )
    return {
        int(payment_id): OpenPspCheckout(
            payment_id=int(payment_id),
            status=str(status),
            has_link=bool((checkout_url or "").strip()),
            created_at=created_at,
        )
        for payment_id, status, checkout_url, created_at in rows.all()
    }


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def reusable_psp_payment(
    pending: list[Payment],
    checkouts: dict[int, OpenPspCheckout],
    *,
    amount: int,
    now: datetime,
) -> Payment | None:
    """Newest pending PSP payment of ``amount`` whose link is fresh enough to show again."""
    for payment in sorted(pending, key=lambda p: int(p.id), reverse=True):
        checkout = checkouts.get(int(payment.id))
        if (
            checkout is None
            or payment.method != PaymentMethod.PSP.value
            or int(payment.amount) != int(amount)
            or checkout.status != SettlementStatus.AWAITING.value
            or not checkout.has_link
            or checkout.created_at is None
        ):
            continue
        if now - _as_utc(checkout.created_at) < PSP_CHECKOUT_REUSE_WINDOW:
            return payment
    return None
