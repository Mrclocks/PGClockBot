"""The staff user card: one customer's account and services, seen from one shop.

Everything is read from the local database (no PasarGuard call), and every figure is
limited to the staff's shop: its purse, its orders and its services. The risk tag is
shown only to the shop that may read the person's private marks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus
from app.services.bot_user_admin import load_user_services
from app.services.color_tags import ColorTag, color_tag_meta
from app.services.daily_report import format_jalali_parts
from app.services.users_ops import ServiceSummary, build_user_ops_row
from app.services.wallet import get_wallet_balance

PAID_ORDER_STATUSES = (
    OrderStatus.PAID.value,
    OrderStatus.DELIVERING.value,
    OrderStatus.DELIVERED.value,
)


@dataclass(slots=True)
class UserCard:
    user: BotUser
    wallet_balance: int
    paid_total: int
    paid_count: int
    joined: str
    risk_tag: ColorTag | None
    services: list[ServiceSummary] = field(default_factory=list)


def _joined_text(created_at: datetime | None) -> str:
    if created_at is None:
        return "—"
    parts = format_jalali_parts(created_at)
    return f"{parts['day']} {parts['month_name']} {parts['year']}"


async def _paid_orders(session: AsyncSession, user_id: int, shop_id: int | None) -> tuple[int, int]:
    shop = Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)
    total, count = (
        await session.execute(
            select(func.coalesce(func.sum(Order.amount), 0), func.count(Order.id)).where(
                Order.user_id == int(user_id),
                shop,
                Order.status.in_(PAID_ORDER_STATUSES),
            )
        )
    ).one()
    return int(total or 0), int(count or 0)


async def build_user_card(
    session: AsyncSession,
    user: BotUser,
    *,
    shop_id: int | None,
    show_private_marks: bool,
) -> UserCard:
    """The card for ``user`` as the shop ``shop_id`` (None = platform) sees them.

    The caller has already checked that the user is in the staff's scope.
    """
    services = await load_user_services(session, int(user.id), shop_id=shop_id)
    row = build_user_ops_row(user, services)
    paid_total, paid_count = await _paid_orders(session, int(user.id), shop_id)
    return UserCard(
        user=user,
        wallet_balance=await get_wallet_balance(session, user, shop_id=shop_id),
        paid_total=paid_total,
        paid_count=paid_count,
        joined=_joined_text(user.created_at),
        risk_tag=color_tag_meta(user.color_tag) if show_private_marks else None,
        services=row.services,
    )
