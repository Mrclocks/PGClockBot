"""Records the command center finds by name or number: customers, resellers, a link's owner, orders.

Each source is one bounded query inside the viewer's shop and reuses the existing staff searches
(``user_search``, ``subscription_lookup``), so a shop never finds another shop's customer, and a
subscription link of another shop answers like an unknown one. Nothing here calls PasarGuard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Role
from app.py_compat import StrEnum
from app.services.formatting import order_status_fa
from app.services.subscription_lookup import find_service_by_link, link_token
from app.services.user_search import search_bot_users, service_owner_clause
from app.services.users_ops import scoped_users_where

# Per source; the command center pages the combined list.
DATA_LIMIT = 8
LABEL_MAX = 48
# «#123»: an order number. A bare number is a Telegram id, searched among people.
ORDER_NUMBER = re.compile(r"#(\d{1,9})")


class DataKind(StrEnum):
    CUSTOMER = "customer"
    RESELLER = "reseller"
    LINK_OWNER = "link_owner"
    ORDER = "order"


@dataclass(frozen=True, slots=True)
class DataScope:
    """What one viewer may search: ``shop_id`` None is the platform shop (the owner)."""

    shop_id: int | None
    customers: bool
    resellers: bool = False
    orders: bool = False


@dataclass(frozen=True, slots=True)
class DataHit:
    kind: DataKind
    entity_id: int
    label: str
    # Plain text; the caller escapes it where it shows it.
    detail: str


def is_subscription_link(query: str) -> bool:
    """True when the query holds a subscription token: a secret never echoed or stored."""
    return link_token(query) is not None


def _person_label(user: BotUser) -> str:
    handle = f"@{user.username}" if user.username else ""
    parts = (user.full_name or "", handle, str(user.telegram_id))
    return " · ".join(part for part in parts if part)[:LABEL_MAX]


def _customer_scope(shop_id: int | None):
    return BotUser.reseller_id.is_(None) if shop_id is None else scoped_users_where(shop_id)


async def _link_owner(session: AsyncSession, query: str, scope: DataScope) -> list[DataHit]:
    match = await find_service_by_link(session, query, shop_id=scope.shop_id)
    if match is None or not scope.customers:
        return []
    detail = f"صاحب سرویس {match.service.pg_username}"
    return [DataHit(DataKind.LINK_OWNER, int(match.owner.id), _person_label(match.owner), detail)]


async def _order(session: AsyncSession, number: int, scope: DataScope) -> list[DataHit]:
    shop_clause = Order.reseller_id.is_(None) if scope.shop_id is None else Order.reseller_id == int(scope.shop_id)
    order = await session.scalar(select(Order).where(Order.id == number, shop_clause))
    if order is None:
        return []
    return [DataHit(DataKind.ORDER, int(order.id), f"سفارش #{order.id}", f"وضعیت: {order_status_fa(order.status)}")]


async def _people(session: AsyncSession, query: str, scope: DataScope) -> list[DataHit]:
    hits: list[DataHit] = []
    if scope.customers:
        customers, _more = await search_bot_users(
            session,
            query,
            scope=_customer_scope(scope.shop_id),
            limit=DATA_LIMIT,
            also=service_owner_clause(query, shop_id=scope.shop_id),
        )
        hits.extend(DataHit(DataKind.CUSTOMER, int(u.id), _person_label(u), "مشتری") for u in customers)
    if scope.resellers:
        resellers, _more = await search_bot_users(
            session, query, scope=BotUser.role == Role.RESELLER.value, limit=DATA_LIMIT
        )
        hits.extend(DataHit(DataKind.RESELLER, int(u.id), _person_label(u), "نماینده") for u in resellers)
    return hits


async def search_data(session: AsyncSession, query: str, scope: DataScope) -> list[DataHit]:
    """Records matching ``query`` inside ``scope``: a link finds only its owner, «#n» only order n."""
    text = (query or "").strip()
    if not text:
        return []
    if is_subscription_link(text):
        return await _link_owner(session, text, scope)
    order_number = ORDER_NUMBER.fullmatch(text)
    if order_number:
        return await _order(session, int(order_number.group(1)), scope) if scope.orders else []
    return await _people(session, text, scope)
