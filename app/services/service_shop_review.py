"""Services whose selling shop could not be derived when ``shop_id`` was introduced.

They are hidden from every shop and from staff actions until the owner assigns one.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, ResellerProfile, UserService
from app.services.shop_membership import ensure_member

REVIEW_LIST_LIMIT = 200


@dataclass(slots=True)
class ReviewRow:
    service_id: int
    customer_id: int
    pg_username: str
    attributed_shop: int | None
    order_shops: list[int | None]


async def list_services_under_review(session: AsyncSession, *, limit: int = REVIEW_LIST_LIMIT) -> list[ReviewRow]:
    """Services waiting for an owner decision, with the evidence for each customer."""
    services = list(
        await session.scalars(
            select(UserService)
            .where(UserService.shop_needs_review.is_(True), UserService.integration_id.is_(None))
            .order_by(UserService.id)
            .limit(limit)
        )
    )
    user_ids = {int(s.bot_user_id) for s in services}
    attribution: dict[int, int | None] = {}
    evidence: dict[int, set[int | None]] = {}
    if user_ids:
        pairs = await session.execute(select(BotUser.id, BotUser.reseller_id).where(BotUser.id.in_(user_ids)))
        attribution = {int(uid): rid for uid, rid in pairs}
        orders = await session.execute(
            select(Order.user_id, Order.reseller_id).where(Order.user_id.in_(user_ids)).distinct()
        )
        for uid, rid in orders:
            evidence.setdefault(int(uid), set()).add(rid)
    return [
        ReviewRow(
            service_id=int(s.id),
            customer_id=int(s.bot_user_id),
            pg_username=s.pg_username,
            attributed_shop=attribution.get(int(s.bot_user_id)),
            order_shops=sorted(evidence.get(int(s.bot_user_id), set()), key=lambda v: (v is None, v or 0)),
        )
        for s in services
    ]


async def assign_service_shop(session: AsyncSession, service_id: int, shop_id: int | None) -> UserService:
    """Give a service under review its shop (None = platform); a second call is refused."""
    if shop_id is not None:
        profile = await session.scalar(select(ResellerProfile.id).where(ResellerProfile.user_id == int(shop_id)))
        if profile is None:
            raise ValueError("فروشگاه یافت نشد")
    claimed = await session.execute(
        update(UserService)
        .where(UserService.id == int(service_id), UserService.shop_needs_review.is_(True), UserService.integration_id.is_(None))
        .values(shop_id=int(shop_id) if shop_id else None, shop_needs_review=False)
    )
    if claimed.rowcount != 1:
        raise ValueError("این سرویس در انتظار تعیین فروشگاه نیست")
    service = await session.get(UserService, int(service_id))
    await ensure_member(session, int(service.bot_user_id), shop_id)
    await session.commit()
    await session.refresh(service)
    return service


async def count_services_under_review(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count()).select_from(UserService).where(UserService.shop_needs_review.is_(True), UserService.integration_id.is_(None))
    )
    return int(result.scalar() or 0)


async def list_assignable_shops(session: AsyncSession) -> list[tuple[int, str]]:
    """(owner user id, label) of every active shop, for the owner's choice."""
    rows = await session.execute(
        select(BotUser.id, BotUser.full_name, BotUser.username)
        .join(ResellerProfile, ResellerProfile.user_id == BotUser.id)
        .where(ResellerProfile.is_active.is_(True))
        .order_by(BotUser.id)
    )
    return [(int(uid), (name or username or f"#{uid}")) for uid, name, username in rows]
