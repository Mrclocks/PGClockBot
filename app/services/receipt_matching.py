"""Suggest pending receipts whose amount matches a payment, inside one shop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    BotUser,
    Order,
    Payment,
    PaymentStatus,
)
from app.services.action_center import payment_shop_clause


async def suggest_receipt_matches(
    session: AsyncSession,
    *,
    payment: Payment,
    window_minutes: int = 120,
    reseller_id: int | None = None,
) -> list[dict[str, Any]]:
    """Soft-match other pending payments with same amount in time window.

    Tenant-scoped: a reseller (``reseller_id`` set) must only ever see matches
    that belong to their own shop (order tenant or payer's shop membership).
    Platform scope (``reseller_id is None``) only sees platform-level payments.
    """
    window_minutes = max(5, min(24 * 60, int(window_minutes or 120)))
    created = payment.created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    lo = created - timedelta(minutes=window_minutes)
    hi = created + timedelta(minutes=window_minutes)
    q = (
        select(Payment)
        .join(BotUser, BotUser.id == Payment.user_id)
        .options(selectinload(Payment.order))
        .where(
            Payment.id != int(payment.id),
            Payment.status == PaymentStatus.PENDING.value,
            Payment.amount == int(payment.amount or 0),
            Payment.created_at >= lo,
            Payment.created_at <= hi,
        )
        .order_by(Payment.created_at.desc())
    )
    q = q.join(Order, Order.id == Payment.order_id, isouter=True).where(payment_shop_clause(reseller_id))
    rows = list((await session.execute(q.limit(5))).scalars().all())
    return [
        {"id": p.id, "order_id": p.order_id, "amount": p.amount, "user_id": p.user_id, "created_at": p.created_at}
        for p in rows
    ]
