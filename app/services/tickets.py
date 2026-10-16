from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Ticket, TicketMessage, TicketStatus
from app.services.ticket_scope import ticket_shop_clause
from app.services.users import current_shop_reseller_id


async def create_ticket(
    session: AsyncSession,
    user_id: int,
    subject: str,
    body: str,
    sender_tg: int,
    *,
    reseller_id: int | None = None,
) -> Ticket:
    """Create a support ticket scoped to the current shop bot (or platform)."""
    shop_rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    ticket = Ticket(
        user_id=user_id,
        subject=subject[:250],
        status=TicketStatus.OPEN.value,
        reseller_id=int(shop_rid) if shop_rid else None,
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketMessage(
            ticket_id=ticket.id,
            sender_id=sender_tg,
            is_staff=False,
            body=body,
        )
    )
    await session.commit()
    await session.refresh(ticket)
    return ticket


async def reply_ticket(
    session: AsyncSession,
    ticket: Ticket,
    body: str,
    sender_tg: int,
    *,
    is_staff: bool,
) -> Ticket:
    if ticket.status == TicketStatus.CLOSED.value:
        raise ValueError("تیکت بسته شده است")
    session.add(
        TicketMessage(
            ticket_id=ticket.id,
            sender_id=sender_tg,
            is_staff=is_staff,
            body=body,
        )
    )
    ticket.status = TicketStatus.ANSWERED.value if is_staff else TicketStatus.OPEN.value
    await session.commit()
    await session.refresh(ticket)
    return ticket


async def close_ticket(session: AsyncSession, ticket: Ticket) -> Ticket:
    ticket.status = TicketStatus.CLOSED.value
    await session.commit()
    await session.refresh(ticket)
    return ticket


async def list_user_tickets(
    session: AsyncSession,
    user_id: int,
    *,
    reseller_id: int | None = None,
) -> list[Ticket]:
    """List tickets for a user, scoped to current shop (or platform when reseller_id is None)."""
    from app.services.users import current_shop_reseller_id

    rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    q = select(Ticket).where(Ticket.user_id == user_id)
    if rid:
        q = q.where(Ticket.reseller_id == int(rid))
    else:
        q = q.where(Ticket.reseller_id.is_(None))
    result = await session.execute(q.order_by(Ticket.id.desc()))
    return list(result.scalars().all())


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    result = await session.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(selectinload(Ticket.messages))
    )
    return result.scalar_one_or_none()


async def list_open_tickets(
    session: AsyncSession,
    limit: int = 50,
    *,
    platform_only: bool = False,
    reseller_id: int | None = None,
) -> list[Ticket]:
    """List open tickets scoped by the shop stamped on each ticket."""
    base = Ticket.status != TicketStatus.CLOSED.value
    if platform_only:
        q = select(Ticket).where(base, ticket_shop_clause(None))
    elif reseller_id is not None:
        q = select(Ticket).where(base, ticket_shop_clause(reseller_id))
    else:
        # No scope given means the platform's own tickets, never every shop's.
        q = select(Ticket).where(base, ticket_shop_clause(None))
    result = await session.execute(q.order_by(Ticket.id.desc()).limit(limit))
    return list(result.scalars().all())
