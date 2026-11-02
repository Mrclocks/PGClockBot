"""Counts the Mini App home shortcuts show as badges, for one customer in one shop.

The home screen loads on every launch, so these are two or three small counts on indexed
columns. Everything is scoped like the screens the badges open: the customer's own tickets
at this desk and this club's rewards (``shop_id`` None = the platform).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.customer_loyalty import count_affordable_rewards
from app.services.customer_support import count_answered_tickets
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HomeBadges:
    """``support``: answered tickets waiting on the customer; ``club``: rewards they can take now."""

    support: int = 0
    club: int = 0

    def payload(self) -> dict[str, int]:
        return asdict(self)


async def home_badges(session: AsyncSession, user: BotUser, *, shop_id: int | None) -> HomeBadges:
    """Badge counts; a failed count shows no badge rather than failing the launch."""
    try:
        return HomeBadges(
            support=await count_answered_tickets(session, int(user.id), shop_id=shop_id),
            club=await count_affordable_rewards(session, user, shop_id=shop_id),
        )
    except SQLAlchemyError:
        await rollback_quiet(session)
        logger.warning("mini app badges failed user=%s", user.id, exc_info=True)
        return HomeBadges()
