"""Keep integration consent and scope evidence out of legacy account erasure."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_link_models import CustomerLinkAction, CustomerLinkChallenge, CustomerTelegramLink
from app.db.integration_models import Integration


async def assert_legacy_user_deletable(session: AsyncSession, user_id: int) -> None:
    retained = or_(
        select(Integration.id).where(Integration.shop_id == user_id).exists(),
        select(CustomerLinkChallenge.id).where(CustomerLinkChallenge.candidate_bot_user_id == user_id).exists(),
        select(CustomerTelegramLink.id).where(CustomerTelegramLink.bot_user_id == user_id).exists(),
        select(CustomerLinkAction.id).where(CustomerLinkAction.actor_bot_user_id == user_id).exists(),
    )
    if await session.scalar(select(retained)):
        raise ValueError("این حساب دارای سابقه یکپارچه‌سازی است؛ حساب را غیرفعال کنید.")
