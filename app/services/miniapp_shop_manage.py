"""Shop-owner Mini App management lists: customers, orders and staff tickets.

Every query is scoped to ``shop_id`` from the verified Mini App scope. Permission
keys come from FEATURE_PERMS: customer list uses ``dashboard`` (same as the bot
and web user list — there is no separate ``users`` key), orders use ``orders``,
tickets use ``tickets``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BotUser, Order, ResellerProfile, Ticket, TicketStatus
from app.services.authz import shop_feature_allowed
from app.services.tickets import reply_ticket
from app.services.ticket_scope import ticket_shop_clause
from app.services.users_ops import scoped_users_where

PAGE_SIZE = 20
PAGE_SIZE_MAX = 20
SEARCH_MAX = 64
ID_MAX = 2**31 - 1
BODY_MAX = 2000

# FEATURE_PERMS keys for each manage surface (no new keys).
PERM_CUSTOMERS = "dashboard"
PERM_ORDERS = "orders"
PERM_TICKETS = "tickets"


class ManageDenied(ValueError):
    """Caller may not use this manage surface."""


class ManageNotFound(ValueError):
    """Row missing in this shop's scope (existence not revealed)."""


def assert_shop_manage_perm(profile: ResellerProfile | None, key: str) -> None:
    if not shop_feature_allowed(key=key, profile=profile):
        raise ManageDenied("دسترسی این بخش برای فروشگاه شما فعال نیست")


def _valid_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManageNotFound("پیدا نشد")
    if not 0 < value <= ID_MAX:
        raise ManageNotFound("پیدا نشد")
    return value


def _page_limit(limit: int | None) -> int:
    if limit is None:
        return PAGE_SIZE
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("تعداد نامعتبر است")
    if not 1 <= limit <= PAGE_SIZE_MAX:
        raise ValueError("تعداد نامعتبر است")
    return limit


def _before_id(value: int | None) -> int | None:
    if value is None:
        return None
    return _valid_id(value)


def _search_text(raw: str | None) -> str:
    text = (raw or "").strip()
    if len(text) > SEARCH_MAX:
        raise ValueError("جستجو خیلی بلند است")
    return text


def _customer_row(user: BotUser) -> dict[str, Any]:
    # Raw text — the Mini App escapes on insert (``esc()``); never trust as HTML.
    return {
        "id": int(user.id),
        "name": user.full_name or "",
        "username": user.username or "",
        "telegram_id": int(user.telegram_id or 0),
        "blocked": bool(user.is_blocked),
    }


def _order_row(order: Order) -> dict[str, Any]:
    return {
        "id": int(order.id),
        "user_id": int(order.user_id),
        "amount": int(order.amount or 0),
        "status": str(order.status or ""),
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def _ticket_row(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": int(ticket.id),
        "user_id": int(ticket.user_id),
        "subject": ticket.subject or "",
        "status": str(ticket.status or ""),
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
    }


async def list_shop_customers(
    session: AsyncSession,
    *,
    shop_id: int,
    profile: ResellerProfile | None,
    before_id: int | None = None,
    limit: int | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    assert_shop_manage_perm(profile, PERM_CUSTOMERS)
    shop = int(shop_id)
    page = _page_limit(limit)
    cursor = _before_id(before_id)
    needle = _search_text(q)
    filters = [scoped_users_where(shop)]
    if cursor is not None:
        filters.append(BotUser.id < cursor)
    if needle:
        like = f"%{needle}%"
        name_match = BotUser.full_name.ilike(like) | BotUser.username.ilike(like)
        if needle.isdigit():
            filters.append(or_(name_match, BotUser.telegram_id == int(needle)))
        else:
            filters.append(name_match)
    rows = list(
        await session.scalars(
            select(BotUser).where(and_(*filters)).order_by(BotUser.id.desc()).limit(page + 1)
        )
    )
    has_more = len(rows) > page
    items = rows[:page]
    return {
        "items": [_customer_row(u) for u in items],
        "next_before_id": int(items[-1].id) if has_more and items else None,
    }


async def list_shop_orders(
    session: AsyncSession,
    *,
    shop_id: int,
    profile: ResellerProfile | None,
    before_id: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    assert_shop_manage_perm(profile, PERM_ORDERS)
    shop = int(shop_id)
    page = _page_limit(limit)
    cursor = _before_id(before_id)
    filters = [Order.reseller_id == shop]
    if cursor is not None:
        filters.append(Order.id < cursor)
    rows = list(
        await session.scalars(
            select(Order).where(and_(*filters)).order_by(Order.id.desc()).limit(page + 1)
        )
    )
    has_more = len(rows) > page
    items = rows[:page]
    return {
        "items": [_order_row(o) for o in items],
        "next_before_id": int(items[-1].id) if has_more and items else None,
    }


async def list_shop_tickets(
    session: AsyncSession,
    *,
    shop_id: int,
    profile: ResellerProfile | None,
    before_id: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    assert_shop_manage_perm(profile, PERM_TICKETS)
    shop = int(shop_id)
    page = _page_limit(limit)
    cursor = _before_id(before_id)
    filters = [ticket_shop_clause(shop), Ticket.status != TicketStatus.CLOSED.value]
    if cursor is not None:
        filters.append(Ticket.id < cursor)
    rows = list(
        await session.scalars(
            select(Ticket).where(and_(*filters)).order_by(Ticket.id.desc()).limit(page + 1)
        )
    )
    has_more = len(rows) > page
    items = rows[:page]
    return {
        "items": [_ticket_row(t) for t in items],
        "next_before_id": int(items[-1].id) if has_more and items else None,
    }


async def shop_ticket_thread(
    session: AsyncSession,
    *,
    shop_id: int,
    profile: ResellerProfile | None,
    ticket_id: int,
) -> dict[str, Any]:
    assert_shop_manage_perm(profile, PERM_TICKETS)
    tid = _valid_id(ticket_id)
    ticket = await session.scalar(
        select(Ticket)
        .options(selectinload(Ticket.messages))
        .where(Ticket.id == tid, ticket_shop_clause(int(shop_id)))
        .limit(1)
    )
    if ticket is None:
        raise ManageNotFound("تیکت پیدا نشد")
    messages = sorted(ticket.messages or [], key=lambda m: int(m.id))
    return {
        "ticket": _ticket_row(ticket),
        "messages": [
            {
                "id": int(m.id),
                "is_staff": bool(m.is_staff),
                "body": m.body or "",
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


async def reply_shop_ticket(
    session: AsyncSession,
    *,
    shop_id: int,
    profile: ResellerProfile | None,
    ticket_id: int,
    staff_telegram_id: int,
    body: str,
) -> dict[str, Any]:
    assert_shop_manage_perm(profile, PERM_TICKETS)
    tid = _valid_id(ticket_id)
    text = (body or "").strip()
    if not text:
        raise ValueError("متن پیام را بنویسید")
    if len(text) > BODY_MAX:
        raise ValueError("متن پیام خیلی بلند است")
    ticket = await session.scalar(
        select(Ticket).where(Ticket.id == tid, ticket_shop_clause(int(shop_id))).limit(1)
    )
    if ticket is None:
        raise ManageNotFound("تیکت پیدا نشد")
    # Re-check shop stamp at mutation time.
    if int(ticket.reseller_id or 0) != int(shop_id):
        raise ManageNotFound("تیکت پیدا نشد")
    await reply_ticket(
        session,
        ticket,
        text,
        int(staff_telegram_id),
        is_staff=True,
    )
    return {"ticket_id": int(ticket.id), "message": "پاسخ ثبت شد"}
