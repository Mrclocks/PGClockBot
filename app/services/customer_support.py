"""Customer support tickets outside the bot chat (Mini App): list, read, open, reply.

Every function takes the ``shop_id`` whose support desk the Mini App is (None is the
platform's): tickets are created with that ``reseller_id``, and only tickets with that same
``reseller_id`` are listed, read or answered, so a shop's customer never reaches another
desk's ticket. Creating and replying go through ``tickets.create_ticket`` and
``tickets.reply_ticket`` so staff see the same tickets and get the same alerts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Ticket, TicketMessage, TicketStatus
from app.services.formatting import ticket_status_fa
from app.services.notifications import notify_new_ticket, notify_ticket_message
from app.services.rich_text import visible_text
from app.services.support_contacts import active_support_contacts, parse_support_contacts, support_chat_url
from app.services.tickets import create_ticket, reply_ticket

logger = logging.getLogger(__name__)

SUBJECT_MAX = 120
BODY_MAX = 2000
TICKET_LIST_LIMIT = 20
THREAD_MESSAGE_LIMIT = 50
# Open (not closed) tickets one customer may have at once.
OPEN_TICKET_LIMIT = 5
# New messages one customer may send per window, across all their tickets.
MESSAGE_RATE_LIMIT = 10
MESSAGE_RATE_WINDOW = timedelta(minutes=10)
_DEFAULT_SUPPORT_TEXT = "پیام خود را بنویسید؛ تیم پشتیبانی پاسخ می‌دهد."


class SupportError(ValueError):
    """A support rule the customer must see (Persian, safe to show)."""


def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def support_overview(ui: dict) -> dict[str, Any]:
    """Support intro text and the t.me contacts the Mini App can open."""
    contacts = []
    for contact in active_support_contacts(parse_support_contacts(ui.get("support_contacts"))):
        url = support_chat_url(contact.get("telegram") or "") or ""
        # Only public t.me links open from a WebView; tg:// user ids do not.
        if url.startswith("https://t.me/"):
            contacts.append({"title": str(contact.get("title") or "پشتیبان")[:40], "url": url})
    return {
        "text": visible_text(ui.get("support_text")) or _DEFAULT_SUPPORT_TEXT,
        "contacts": contacts,
    }


def _ticket_row(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": int(ticket.id),
        "subject": ticket.subject or "",
        "status": ticket.status,
        "status_fa": ticket_status_fa(ticket.status),
        "is_closed": ticket.status == TicketStatus.CLOSED.value,
        "updated_at": _iso(ticket.updated_at or ticket.created_at),
    }


def _customer_ticket_clause(user_id: int, shop_id: int | None):
    desk = Ticket.reseller_id.is_(None) if shop_id is None else Ticket.reseller_id == int(shop_id)
    return (Ticket.user_id == int(user_id)) & desk


async def list_customer_tickets(
    session: AsyncSession, user_id: int, *, shop_id: int | None, limit: int = TICKET_LIST_LIMIT
) -> list[dict]:
    """The customer's tickets at this desk, newest first."""
    rows = await session.scalars(
        select(Ticket).where(_customer_ticket_clause(user_id, shop_id)).order_by(Ticket.id.desc()).limit(limit)
    )
    return [_ticket_row(t) for t in rows]


async def count_answered_tickets(session: AsyncSession, user_id: int, *, shop_id: int | None) -> int:
    """The customer's tickets at this desk that staff answered and that wait on the customer."""
    count = await session.scalar(
        select(func.count(Ticket.id)).where(
            _customer_ticket_clause(user_id, shop_id), Ticket.status == TicketStatus.ANSWERED.value,
        )
    )
    return int(count or 0)


async def _own_ticket(session: AsyncSession, user_id: int, shop_id: int | None, ticket_id: int) -> Ticket:
    ticket = await session.scalar(
        select(Ticket).where(Ticket.id == int(ticket_id), _customer_ticket_clause(user_id, shop_id))
    )
    if ticket is None:
        raise SupportError("تیکت پیدا نشد")
    return ticket


async def customer_ticket_thread(
    session: AsyncSession, user_id: int, ticket_id: int, *, shop_id: int | None
) -> dict[str, Any]:
    """One of the customer's tickets at this desk with its latest messages, oldest first."""
    ticket = await _own_ticket(session, user_id, shop_id, ticket_id)
    latest = list(await session.scalars(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket.id)
        .order_by(TicketMessage.id.desc())
        .limit(THREAD_MESSAGE_LIMIT)
    ))
    return {
        **_ticket_row(ticket),
        "messages": [
            {"id": int(m.id), "from_staff": bool(m.is_staff), "body": m.body or "", "created_at": _iso(m.created_at)}
            for m in reversed(latest)
        ],
    }


def _clean_text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SupportError(f"{field} را بنویسید")
    text = value.replace("\r\n", "\n").strip()
    if not text:
        raise SupportError(f"{field} را بنویسید")
    if len(text) > limit:
        raise SupportError(f"{field} حداکثر {limit} حرف است")
    return text


async def _assert_message_rate(session: AsyncSession, telegram_id: int, now: datetime) -> None:
    sent = await session.scalar(
        select(func.count())
        .select_from(TicketMessage)
        .where(
            TicketMessage.sender_id == int(telegram_id),
            TicketMessage.is_staff.is_(False),
            TicketMessage.created_at >= now - MESSAGE_RATE_WINDOW,
        )
    )
    if int(sent or 0) >= MESSAGE_RATE_LIMIT:
        raise SupportError("پیام‌های زیادی فرستاده‌اید؛ چند دقیقه بعد دوباره تلاش کنید")


@dataclass(slots=True)
class SupportActor:
    """The customer acting, from the authenticated Mini App user, and the desk they write to."""

    user_id: int
    telegram_id: int
    name: str | None
    shop_id: int | None = None

    @classmethod
    def of(cls, user: BotUser, *, shop_id: int | None) -> SupportActor:
        return cls(int(user.id), int(user.telegram_id), user.full_name or user.username, shop_id)


async def open_customer_ticket(
    session: AsyncSession, actor: SupportActor, *, subject: object, body: object, now: datetime | None = None,
) -> Ticket:
    """Create a ticket at the actor's desk; raises ``SupportError`` before writing anything."""
    clean_subject = _clean_text(subject, field="موضوع", limit=SUBJECT_MAX)
    clean_body = _clean_text(body, field="متن پیام", limit=BODY_MAX)
    await _assert_message_rate(session, actor.telegram_id, now or datetime.now(timezone.utc))
    open_count = await session.scalar(
        select(func.count()).select_from(Ticket).where(
            _customer_ticket_clause(actor.user_id, actor.shop_id), Ticket.status != TicketStatus.CLOSED.value,
        )
    )
    if int(open_count or 0) >= OPEN_TICKET_LIMIT:
        raise SupportError("چند تیکت باز دارید؛ در همان تیکت‌ها ادامه دهید")
    # create_ticket reads None as "the current shop"; the platform desk only runs without one.
    return await create_ticket(
        session, actor.user_id, clean_subject, clean_body, actor.telegram_id, reseller_id=actor.shop_id
    )


async def reply_customer_ticket(
    session: AsyncSession, actor: SupportActor, ticket_id: int, *, body: object, now: datetime | None = None,
) -> Ticket:
    """Add the customer's message to their own open ticket at the actor's desk."""
    clean_body = _clean_text(body, field="متن پیام", limit=BODY_MAX)
    ticket = await _own_ticket(session, actor.user_id, actor.shop_id, ticket_id)
    if ticket.status == TicketStatus.CLOSED.value:
        raise SupportError("این تیکت بسته شده است؛ برای ادامه، تیکت تازه بسازید")
    await _assert_message_rate(session, actor.telegram_id, now or datetime.now(timezone.utc))
    return await reply_ticket(session, ticket, clean_body, actor.telegram_id, is_staff=False)


async def notify_staff(bot: Bot, session: AsyncSession, actor: SupportActor, ticket: Ticket, *, body: str | None) -> None:
    """Alert the desk's staff like the bot does (``bot`` is that desk's bot); a failed alert
    never undoes the saved message."""
    ticket_id, subject = int(ticket.id), ticket.subject
    try:
        if body is None:
            await notify_new_ticket(
                bot, session, ticket_id=ticket_id, subject=subject, user_name=actor.name,
                ticket_user_id=actor.user_id, ticket_reseller_id=actor.shop_id,
            )
        else:
            await notify_ticket_message(
                bot, session, ticket_id=ticket_id, subject=subject, body=body, from_staff=False,
                ticket_user_id=actor.user_id, actor_name=actor.name, ticket_reseller_id=actor.shop_id,
            )
    except Exception:
        # Best-effort alert: the ticket is already committed and listed for staff.
        logger.warning("mini app ticket alert failed ticket=%s", ticket_id, exc_info=True)
