"""Remind staff of support tickets that wait for an answer longer than the shop allows.

A shop (or the platform) turns reminders on with ``ticket_reminder_hours``. A ticket is due when
it is open (the customer wrote last) and its last message is older than that. Each waiting
period is reminded once: the job claims the ticket by setting ``reminded_at`` before it sends,
and a new customer message starts a new waiting period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerSetting, Setting, Ticket, TicketMessage, TicketStatus

SETTING_KEY = "ticket_reminder_hours"
DEFAULT_HOURS = "0"
MAX_HOURS = 168
# Tickets reminded per run; the job runs again shortly, so a long backlog drains over a few runs.
BATCH_LIMIT = 50
# Upper bound on shops read per run; far above any real installation.
SHOP_LIMIT = 10_000
PLATFORM_KEY = 0


@dataclass(frozen=True, slots=True)
class DueTicket:
    ticket_id: int
    user_id: int
    shop_id: int | None
    subject: str
    waiting_since: datetime


def reminder_hours(raw: object) -> int:
    """The configured hours, 0 (off) for anything that is not a whole number in range."""
    try:
        hours = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return hours if 0 < hours <= MAX_HOURS else 0


async def reminder_cutoffs(session: AsyncSession, *, now: datetime) -> dict[int, datetime]:
    """For each shop with reminders on (``PLATFORM_KEY`` = the platform), the newest due message time."""
    hours: dict[int, int] = {}
    platform = await session.scalar(select(Setting.value).where(Setting.key == SETTING_KEY))
    if reminder_hours(platform):
        hours[PLATFORM_KEY] = reminder_hours(platform)
    rows = await session.execute(
        select(ResellerSetting.reseller_user_id, ResellerSetting.value)
        .where(ResellerSetting.key == SETTING_KEY)
        .limit(SHOP_LIMIT)
    )
    for shop_id, value in rows:
        if reminder_hours(value):
            hours[int(shop_id)] = reminder_hours(value)
    return {shop: now - timedelta(hours=value) for shop, value in hours.items()}


async def due_tickets(
    session: AsyncSession, cutoffs: dict[int, datetime], *, limit: int = BATCH_LIMIT
) -> list[DueTicket]:
    """Open tickets of shops with reminders on, waiting past their shop's cutoff, oldest first."""
    if not cutoffs:
        return []
    shop_key = func.coalesce(Ticket.reseller_id, PLATFORM_KEY)
    open_in_scope = select(Ticket.id).where(
        Ticket.status == TicketStatus.OPEN.value, shop_key.in_(list(cutoffs))
    )
    # Only messages of the open tickets in scope are aggregated, never the whole table.
    last_message = (
        select(TicketMessage.ticket_id, func.max(TicketMessage.created_at).label("last_at"))
        .where(TicketMessage.ticket_id.in_(open_in_scope))
        .group_by(TicketMessage.ticket_id)
        .subquery()
    )
    waiting_since = func.coalesce(last_message.c.last_at, Ticket.created_at)
    cutoff = case(cutoffs, value=shop_key)
    rows = await session.execute(
        select(Ticket.id, Ticket.user_id, Ticket.reseller_id, Ticket.subject, waiting_since.label("since"))
        .outerjoin(last_message, last_message.c.ticket_id == Ticket.id)
        .where(
            Ticket.status == TicketStatus.OPEN.value,
            shop_key.in_(list(cutoffs)),
            waiting_since <= cutoff,
            or_(Ticket.reminded_at.is_(None), Ticket.reminded_at < waiting_since),
        )
        .order_by(waiting_since, Ticket.id)
        .limit(limit)
    )
    return [
        DueTicket(int(tid), int(uid), int(rid) if rid else None, subject or "", since)
        for tid, uid, rid, subject, since in rows
    ]


async def claim_reminder(session: AsyncSession, due: DueTicket, *, now: datetime) -> bool:
    """Mark this waiting period reminded; False when another run did, or the ticket moved on."""
    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == due.ticket_id,
            Ticket.status == TicketStatus.OPEN.value,
            or_(Ticket.reminded_at.is_(None), Ticket.reminded_at < due.waiting_since),
        )
        # Keep updated_at: a reminder is not activity on the ticket.
        .values(reminded_at=now, updated_at=Ticket.updated_at)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1
