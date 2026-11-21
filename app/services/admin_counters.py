"""Shop-scoped pending-queue counts for admin / reseller hub badges.

Predicates match ``finance_reports.shop_ops_snapshot`` / ``home_overview``
(wallet_shop_id + Order.reseller_id) so platform and shop never share traffic.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Payment, PaymentStatus, Ticket, TicketStatus
from app.services.demo_users import non_demo_customer
from app.services.numbers import to_fa_digits
from app.services.ticket_scope import ticket_shop_clause
from app.services.service_cancellations import pending_cancellation_count
from app.services.delivery_failures import count_stuck_paid_orders


@dataclass(frozen=True, slots=True)
class PendingCounts:
    """Operator queue sizes for one shop (``shop_id=None`` = platform)."""

    payments: int = 0
    tickets: int = 0
    cancellations: int = 0
    # Paid orders still without a service: delivery failed or is waiting for a retry.
    deliveries: int = 0

    @property
    def total(self) -> int:
        return int(self.payments) + int(self.tickets) + int(self.cancellations) + int(self.deliveries)


def _payment_scope(shop_id: int | None):
    """Receipt queue scope — same tenancy as finance / dashboard summaries."""
    if shop_id is None:
        return or_(
            and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id.is_(None)),
            and_(Payment.is_wallet_topup.is_(False), Order.reseller_id.is_(None)),
        )
    rid = int(shop_id)
    return or_(
        and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id == rid),
        and_(Payment.is_wallet_topup.is_(False), Order.reseller_id == rid),
    )


async def count_pending_payments(
    session: AsyncSession, *, shop_id: int | None
) -> int:
    q = (
        select(func.count())
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            non_demo_customer(Payment.user_id),
            _payment_scope(shop_id),
        )
    )
    return int((await session.execute(q)).scalar() or 0)


async def count_open_tickets(session: AsyncSession, *, shop_id: int | None) -> int:
    """Open + answered (matches ``list_open_tickets`` / bot ticket lists)."""
    base = Ticket.status != TicketStatus.CLOSED.value
    q = (
        select(func.count())
        .select_from(Ticket)
        .outerjoin(BotUser, BotUser.id == Ticket.user_id)
        .where(base, or_(BotUser.id.is_(None), BotUser.is_demo.is_(False)))
    )
    q = q.where(ticket_shop_clause(shop_id))
    return int((await session.execute(q)).scalar() or 0)


async def pending_counts(
    session: AsyncSession, *, shop_id: int | None
) -> PendingCounts:
    """All hub badges for one tenant. ``shop_id`` is required keyword (None=platform)."""
    payments = await count_pending_payments(session, shop_id=shop_id)
    tickets = await count_open_tickets(session, shop_id=shop_id)
    cancellations = await pending_cancellation_count(session, shop_id)
    deliveries = await count_stuck_paid_orders(session, reseller_id=shop_id)
    return PendingCounts(
        payments=payments, tickets=tickets, cancellations=cancellations, deliveries=deliveries
    )


def badge_suffix(count: int) -> str:
    """`` (۳)`` when count > 0; empty otherwise."""
    n = int(count or 0)
    if n <= 0:
        return ""
    return f" ({to_fa_digits(n)})"


def with_badge(label: str, count: int) -> str:
    return f"{label}{badge_suffix(count)}"


def format_queue_summary(counts: PendingCounts) -> str | None:
    """One-line Persian summary for hub body text, or None when queue is empty."""
    if counts.total <= 0:
        return None
    parts = [
        f"رسید {to_fa_digits(counts.payments)}",
        f"تیکت {to_fa_digits(counts.tickets)}",
        f"لغو {to_fa_digits(counts.cancellations)}",
    ]
    if counts.deliveries:
        parts.append(f"تحویل ناموفق {to_fa_digits(counts.deliveries)}")
    return "⏳ صف: " + " · ".join(parts)
