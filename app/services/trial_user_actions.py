"""Block, message and «تست دوباره» actions on the customers listed as trial users.

Both actions only ever reach customers who took a trial from the acting shop. Blocking is
a flag on the person, so it also stops them in every other shop and stays with the
platform Owner; a shop can message its own trial takers through its own bot.
"""

from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    CampaignRecipient,
    MarketingPreference,
    Order,
    TargetedCampaign,
    TrialClaim,
    UserService,
)
from app.services.campaigns import clean_campaign_text
from app.services.platform_identity import is_explicit_owner_staff
from app.services.table_bulk import MAX_BULK, bulk_toggle_block, parse_bulk_ids
from app.services.staff_audit import actor_from_staff, add_staff_action
from app.services.trial_users import is_buyer_clause, trial_orders_subquery, trial_state

SELECTED_AUDIENCE = "selected"
ACTOR_LIMIT = 128


@dataclass(slots=True)
class RetrialResult:
    reset: int
    refused: int


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


async def _campaign_for_key(session: AsyncSession, shop_id: int | None, request_key: str) -> TargetedCampaign | None:
    shop_clause = TargetedCampaign.reseller_id.is_(None) if shop_id is None else TargetedCampaign.reseller_id == int(shop_id)
    return await session.scalar(select(TargetedCampaign).where(TargetedCampaign.request_key == request_key, shop_clause))


async def message_trial_users(
    session: AsyncSession, *, staff: dict, shop_id: int | None, user_ids: list[int], text: str,
    request_key: str | None = None,
) -> TargetedCampaign:
    """Queue one message to the chosen trial takers through the shop's own bot.

    Customers who blocked or opted out of marketing are left out of the snapshot; the
    sender checks both again at send time. A repeated ``request_key`` of the same shop returns
    the campaign it already created instead of queuing a second one.
    """
    if request_key and (existing := await _campaign_for_key(session, shop_id, request_key)):
        return existing
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
        request_key=request_key,
    )
    session.add(campaign)
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent call with the same key won the unique index: hand back its campaign.
        await session.rollback()
        if request_key and (existing := await _campaign_for_key(session, shop_id, request_key)):
            return existing
        raise
    session.add_all([CampaignRecipient(campaign_id=campaign.id, user_id=uid) for uid in recipients])
    await session.commit()
    return campaign


def trial_shop_key(shop_id: int | None) -> str:
    """The ``TrialClaim.shop_key`` of a shop, as ``create_order`` writes it."""
    return "platform" if shop_id is None else str(int(shop_id))


async def retrial_eligible_ids(session: AsyncSession, *, shop_id: int | None, user_ids: list[int]) -> set[int]:
    """Who of ``user_ids`` may take this shop's trial again.

    A customer of this shop's trial who never bought here, is not blocked, and whose trial service has ended
    (or is gone): someone still on a trial would hold two at once, and a buyer is no longer a trial lead.
    """
    trials = trial_orders_subquery(shop_id)
    rows = await session.execute(
        select(BotUser.id, UserService)
        .join(trials, trials.c.user_id == BotUser.id)
        .join(Order, Order.id == trials.c.order_id)
        .outerjoin(UserService, UserService.id == Order.service_id)
        .where(BotUser.id.in_(user_ids), BotUser.is_blocked.is_(False), ~is_buyer_clause(shop_id, BotUser.id))
    )
    now = datetime.now(timezone.utc)
    return {
        int(uid) for uid, service in rows.all()
        if service is None or service.is_cancelled or trial_state(service, now=now)[0] == "ended"
    }


async def reset_trials(session: AsyncSession, *, staff: dict, shop_id: int | None, user_ids: list[int]) -> RetrialResult:
    """Let the chosen trial takers of this shop take its trial again; their past orders and services stay."""
    wanted = parse_bulk_ids(user_ids)
    eligible = await retrial_eligible_ids(session, shop_id=shop_id, user_ids=wanted) if wanted else set()
    reset = 0
    if eligible:
        result = await session.execute(
            delete(TrialClaim).where(TrialClaim.shop_key == trial_shop_key(shop_id), TrialClaim.user_id.in_(eligible))
            .execution_options(synchronize_session=False)
        )
        reset = int(result.rowcount or 0)
    if reset:
        add_staff_action(session, actor_from_staff(staff, shop_id=shop_id), action="trial_reset",
                         target_type="customer", target_id=None, reason=f"{reset} کاربر")
    await session.commit()
    return RetrialResult(reset=reset, refused=len(wanted) - reset)

