"""Per-shop memory of customers who asked not to see the home offer to add a previous service."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ServiceImportOfferDismissal


def _shop_key(shop_id: int | None) -> int:
    return int(shop_id) if shop_id else 0


async def is_offer_dismissed(session: AsyncSession, user_id: int, shop_id: int | None) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    ServiceImportOfferDismissal.user_id == int(user_id),
                    ServiceImportOfferDismissal.shop_key == _shop_key(shop_id),
                )
            )
        )
    )


async def dismiss_offer(session: AsyncSession, user_id: int, shop_id: int | None) -> None:
    """Record the choice; asking twice (or two taps at once) leaves one row."""
    if await is_offer_dismissed(session, user_id, shop_id):
        return
    try:
        async with session.begin_nested():
            session.add(ServiceImportOfferDismissal(user_id=int(user_id), shop_key=_shop_key(shop_id)))
    except IntegrityError:
        return  # the other tap won the race; the row exists
    await session.commit()
