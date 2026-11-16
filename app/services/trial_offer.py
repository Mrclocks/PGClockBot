"""Whether the home screen may offer a free trial to a customer, and which trial plans it would start."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, TrialClaim
from app.services.orders import list_active_plans
from app.services.users import current_shop_reseller_id, on


def _trial_shop_key() -> str:
    """Same key `create_order` uses for the one-trial-per-shop claim."""
    shop_id = current_shop_reseller_id()
    return str(int(shop_id)) if shop_id is not None else "platform"


async def has_claimed_trial(session: AsyncSession, user_id: int) -> bool:
    row = await session.scalar(
        select(TrialClaim.id)
        .where(TrialClaim.user_id == int(user_id), TrialClaim.shop_key == _trial_shop_key())
        .limit(1)
    )
    return row is not None


async def offerable_trial_plans(session: AsyncSession, ui: Mapping[str, str] | None, user_id: int) -> list[Plan]:
    """Active trial plans of this shop, empty when trials are off or this customer already took theirs."""
    if not on((ui or {}).get("trial_enabled")):
        return []
    if await has_claimed_trial(session, user_id):
        return []
    plans = await list_active_plans(session, include_trial=True)
    return [plan for plan in plans if plan.is_trial]
