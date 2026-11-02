"""Block and message actions on the customers listed as trial users.

Both actions only ever reach customers who took a trial from the acting shop. Blocking is
a flag on the person, so it also stops them in every other shop and stays with the
platform Owner; a shop can message its own trial takers through its own bot.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, CampaignRecipient, MarketingPreference, TargetedCampaign
from app.services.campaigns import clean_campaign_text
from app.services.platform_identity import is_explicit_owner_staff
from app.services.table_bulk import MAX_BULK, bulk_toggle_block, parse_bulk_ids
from app.services.trial_users import trial_orders_subquery

SELECTED_AUDIENCE = "selected"
ACTOR_LIMIT = 128


@dataclass(slots=True)
class BlockResult:
    changed: int
    refused: int


async def trial_taker_ids(session: AsyncSession, *, shop_id: int | None, user_ids: list[int]) -> set[int]:
    """The requested ids that took a trial from this shop; every other id is dropped."""
    if not user_ids:
        return set()
    trials = trial_orders_subquery(shop_id)
    rows = await session.scalars(select(trials.c.user_id).where(trials.c.user_id.in_(user_ids)))
    return {int(uid) for uid in rows.all()}


def _actor_label(staff: dict) -> str:
    return str(staff.get("username") or staff.get("bot_user_id") or "operator")[:ACTOR_LIMIT]


async def block_trial_users(
    session: AsyncSession, *, staff: dict, shop_id: int | None, user_ids: list[int], block: bool
) -> BlockResult:
    """Block or unblock trial takers; only the platform Owner may, because the flag is global."""
    if not is_explicit_owner_staff(staff):
        raise ValueError("مسدود کردن فقط برای مالک پلتفرم مجاز است")
    wanted = parse_bulk_ids(user_ids)
    allowed = await trial_taker_ids(session, shop_id=shop_id, user_ids=wanted)
    ids = [uid for uid in wanted if uid in allowed]
    changed, refused = await bulk_toggle_block(session, staff, ids, block=block) if ids else (0, 0)
    return BlockResult(changed=changed, refused=refused + len(wanted) - len(ids))


async def message_trial_users(
    session: AsyncSession, *, staff: dict, shop_id: int | None, user_ids: list[int], text: str
) -> TargetedCampaign:
    """Queue one message to the chosen trial takers through the shop's own bot.

    Customers who blocked or opted out of marketing are left out of the snapshot; the
    sender checks both again at send time.
    """
    body = clean_campaign_text(text)
    wanted = parse_bulk_ids(user_ids)
    allowed = await trial_taker_ids(session, shop_id=shop_id, user_ids=wanted)
    opted_out = select(MarketingPreference.user_id).where(
        MarketingPreference.shop_key == (shop_id or 0), MarketingPreference.enabled.is_(False)
    )
    recipients = list(
        (
            await session.scalars(
                select(BotUser.id)
                .where(
                    BotUser.id.in_(allowed or {0}),
                    BotUser.is_blocked.is_(False),
                    BotUser.id.not_in(opted_out),
                )
                .order_by(BotUser.id)
                .limit(MAX_BULK)
            )
        ).all()
    )
    if not recipients:
        raise ValueError("گیرنده‌ای که پیام بازاریابی بپذیرد پیدا نشد")
    campaign = TargetedCampaign(
        reseller_id=shop_id,
        audience=SELECTED_AUDIENCE,
        days=1,
        text=body,
        status="running",
        created_by=_actor_label(staff),
    )
    session.add(campaign)
    await session.flush()
    session.add_all([CampaignRecipient(campaign_id=campaign.id, user_id=uid) for uid in recipients])
    await session.commit()
    return campaign
