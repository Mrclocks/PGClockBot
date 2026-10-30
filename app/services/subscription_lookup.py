"""Find the owner of a subscription from its link or token, inside one shop.

Used by staff searches. The match is scoped by the shop that sold the service, so a link of another
shop is indistinguishable from an unknown one. The token is a bearer secret: it is only compared
in the query and never logged, returned or stored by this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Plan, UserService
from app.services.service_import import parse_subscription_link

# A bare token this long cannot be a Telegram username (32 at most) or a numeric id.
BARE_TOKEN_MIN_LENGTH = 33


@dataclass(slots=True)
class SubscriptionMatch:
    service: UserService
    owner: BotUser
    plan_name: str | None


def link_token(raw: str | None) -> str | None:
    """The token when ``raw`` is a subscription URL or an unmistakable bare token, else None."""
    link = parse_subscription_link(raw)
    if link is None:
        return None
    if link.prefix is None and len(link.token) < BARE_TOKEN_MIN_LENGTH:
        return None
    return link.token


async def find_service_by_link(session: AsyncSession, raw: str | None, *, shop_id: int | None) -> SubscriptionMatch | None:
    """The service sold by ``shop_id`` (None = platform) whose subscription token is in ``raw``."""
    token = link_token(raw)
    if token is None:
        return None
    shop_clause = UserService.shop_id.is_(None) if shop_id is None else UserService.shop_id == int(shop_id)
    row = (
        await session.execute(
            select(UserService, BotUser, Plan.name)
            .join(BotUser, BotUser.id == UserService.bot_user_id)
            .outerjoin(Plan, Plan.id == UserService.plan_id)
            .where(
                UserService.subscription_token == token,
                UserService.shop_needs_review.is_(False),
                shop_clause,
            )
            .order_by(UserService.id)
            .limit(1)
        )
    ).first()
    return SubscriptionMatch(row[0], row[1], row[2]) if row else None
