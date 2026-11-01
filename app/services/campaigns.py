"""Shop-scoped audience snapshots and resumable Telegram campaigns."""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import BotUser, CampaignRecipient, MarketingPreference, Order, Plan, ShopBroadCampaignDay, TargetedCampaign, UserService
from app.services.daily_cap import claim_daily_cap
from app.services.service_scope import shop_services_clause
from app.services.shop_customer_block import not_blocked_in_shop_clause
from app.services.shop_membership import shop_member_clause

AUDIENCES = {
    "trial": "کاربران تست بدون خرید",
    "expiring": "سرویس‌های نزدیک انقضا",
    "inactive": "مشتریان بدون خرید در بازهٔ انتخابی",
    "active": "دارندگان سرویس فعال",
    "all": "همهٔ مشتریان فروشگاه",
}
# Reach a shop's whole audience: at most this many per shop per UTC day, so a shop bot is
# not reported as spam. The platform has its own broadcast and is not counted here.
BROAD_AUDIENCES = frozenset({"active", "all"})
BROAD_PER_DAY = 1
BROAD_LIMIT_MESSAGE = "مخاطبان گسترده (همهٔ مشتریان یا دارندگان سرویس فعال) برای هر فروشگاه روزی یک کمپین است"
RECIPIENT_LIMIT = 5000
CAMPAIGN_LIST_LIMIT = 5


def _member_clause(shop_id):
    """Customers of the shop: attributed, recorded as members, or with an order there."""
    placed = exists(select(Order.id).where(Order.user_id == BotUser.id, Order.reseller_id == shop_id))
    return or_(shop_member_clause(shop_id), placed)


def audience_query(*, shop_id, audience, days, now=None):
    if audience not in AUDIENCES or not 1 <= days <= 365:
        raise ValueError("گروه مخاطبان یا تعداد روز نامعتبر است")
    now = now or datetime.now(timezone.utc)
    shop_order = and_(Order.user_id == BotUser.id, Order.reseller_id == shop_id)
    bought = exists(select(Order.id).where(shop_order, Order.status == "delivered", Order.amount > 0))
    member = _member_clause(shop_id)
    opted_out = exists(select(MarketingPreference.user_id).where(
        MarketingPreference.user_id == BotUser.id, MarketingPreference.shop_key == (shop_id or 0), MarketingPreference.enabled.is_(False)))
    query = select(BotUser.id).where(
        member, BotUser.is_blocked.is_(False), not_blocked_in_shop_clause(shop_id), BotUser.role.in_(["user", "reseller"]), ~opted_out
    )
    return query.where(*_audience_filters(audience, shop_id=shop_id, days=days, now=now, bought=bought, shop_order=shop_order)).order_by(BotUser.id)


def _active_service(shop_id, now):
    return and_(
        UserService.bot_user_id == BotUser.id, shop_services_clause(shop_id),
        UserService.is_cancelled.is_(False), UserService.cancellation_pending.is_(False),
        UserService.quota_status == "active",
        UserService.quota_synced_at >= now - timedelta(days=1),
        UserService.quota_expire_at >= now,
    )


def _audience_filters(audience, *, shop_id, days, now, bought, shop_order):
    """Extra WHERE clauses that pick one audience out of the shop's reachable customers."""
    if audience == "trial":
        return [~bought, exists(select(Order.id).join(Plan, Plan.id == Order.plan_id).where(
            shop_order, Order.status == "delivered", Plan.is_trial.is_(True)))]
    if audience == "inactive":
        cutoff = now - timedelta(days=days)
        return [BotUser.created_at <= cutoff, ~exists(select(Order.id).where(
            shop_order, Order.status == "delivered", Order.created_at > cutoff))]
    if audience == "active":
        return [exists(select(UserService.id).where(_active_service(shop_id, now)))]
    if audience == "all":
        return []
    return [exists(select(UserService.id).where(
        _active_service(shop_id, now), UserService.quota_expire_at <= now + timedelta(days=days)))]


async def audience_count(session, **kwargs):
    return int(await session.scalar(select(func.count()).select_from(audience_query(**kwargs).subquery())) or 0)


def clean_campaign_text(raw) -> str:
    """Plain Telegram text of a campaign (sent with parse_mode=None), or ValueError."""
    text = str(raw or "").replace("\x00", "").strip()
    if not text or len(text) > 3500:
        raise ValueError("متن پیام باید بین ۱ تا ۳۵۰۰ نویسه باشد")
    # Reject HTML-ish payloads that would confuse operators if ever shown elsewhere
    # with richer rendering.
    if "<script" in text.lower() or "javascript:" in text.lower():
        raise ValueError("متن پیام نامعتبر است")
    return text


async def create_campaign(session, *, shop_id, audience, days, text, actor):
    text = clean_campaign_text(text)
    ids = list((await session.scalars(audience_query(shop_id=shop_id, audience=audience, days=days).limit(RECIPIENT_LIMIT))).all())
    if not ids:
        raise ValueError("مخاطبی در این گروه موجود نیست")
    if audience in BROAD_AUDIENCES and shop_id:
        if not await claim_daily_cap(session, ShopBroadCampaignDay, shop_id=int(shop_id), amount=1, cap=BROAD_PER_DAY):
            raise ValueError(BROAD_LIMIT_MESSAGE)
    row = TargetedCampaign(reseller_id=shop_id, audience=audience, days=days, text=text, created_by=actor)
    session.add(row)
    await session.flush()
    session.add_all([CampaignRecipient(campaign_id=row.id, user_id=uid) for uid in ids])
    await session.commit()
    return row


async def change_campaign_state(session, campaign_id, *, shop_id, action):
    transitions = {"start": (["draft", "paused"], "running"), "pause": (["running"], "paused")}
    if action not in transitions:
        raise ValueError("عملیات کمپین نامعتبر است")
    before, after = transitions[action]
    changed = await session.execute(update(TargetedCampaign).where(
        TargetedCampaign.id == campaign_id, TargetedCampaign.reseller_id == shop_id,
        TargetedCampaign.status.in_(before),
    ).values(status=after))
    if changed.rowcount != 1:
        raise ValueError("کمپین یافت نشد یا وضعیت آن تغییر کرده است")
    await session.commit()


async def set_marketing_preference(session, user_id, *, shop_id, enabled):
    key = (user_id, shop_id or 0)
    row = await session.get(MarketingPreference, key)
    if row:
        row.enabled = enabled
    else:
        session.add(MarketingPreference(user_id=user_id, shop_key=shop_id or 0, enabled=enabled))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await session.execute(update(MarketingPreference).where(
            MarketingPreference.user_id == user_id, MarketingPreference.shop_key == (shop_id or 0),
        ).values(enabled=enabled))
        await session.commit()


async def list_recent_campaigns(session, *, shop_id, limit=CAMPAIGN_LIST_LIMIT):
    """Newest campaigns of one shop (``None`` = platform), bounded."""
    owner = TargetedCampaign.reseller_id.is_(None) if shop_id is None else TargetedCampaign.reseller_id == int(shop_id)
    query = select(TargetedCampaign).where(owner).order_by(TargetedCampaign.id.desc()).limit(max(1, int(limit)))
    return list((await session.scalars(query)).all())


async def campaign_progress_map(session, campaign_ids):
    """Recipient status counts for several campaigns in one query."""
    ids = [int(campaign_id) for campaign_id in campaign_ids]
    progress = {campaign_id: {} for campaign_id in ids}
    if not ids:
        return progress
    rows = await session.execute(
        select(CampaignRecipient.campaign_id, CampaignRecipient.status, func.count())
        .where(CampaignRecipient.campaign_id.in_(ids))
        .group_by(CampaignRecipient.campaign_id, CampaignRecipient.status)
    )
    for campaign_id, status, count in rows.all():
        progress[int(campaign_id)][status] = int(count)
    return progress


async def campaign_progress(session, campaign_id):
    return dict((await session.execute(select(CampaignRecipient.status, func.count()).where(
        CampaignRecipient.campaign_id == campaign_id).group_by(CampaignRecipient.status))).all())


def campaign_send_delay(audience: str, *, requested: float) -> float:
    """Price announcements leave a full second between sends, including adjacent campaigns."""
    if requested == 0:
        return 0
    return max(1.0, requested) if audience == "catalog_discount" else requested


async def process_campaign(session, campaign_id, bot, *, batch_size=20, delay=0.1):
    now, token = datetime.now(timezone.utc), secrets.token_hex(16)
    claim = await session.execute(update(TargetedCampaign).where(
        TargetedCampaign.id == campaign_id, TargetedCampaign.status == "running",
        or_(TargetedCampaign.locked_until.is_(None), TargetedCampaign.locked_until < now),
    ).values(lock_token=token, locked_until=now + timedelta(minutes=5)).execution_options(synchronize_session=False))
    await session.commit()
    if claim.rowcount != 1:
        return
    campaign = await session.get(TargetedCampaign, campaign_id)
    await session.refresh(campaign)
    shop_id, text = campaign.reseller_id, campaign.text
    delay = campaign_send_delay(campaign.audience, requested=delay)
    # A send interrupted by process death has unknown delivery; never auto-send it twice.
    await session.execute(update(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.status == "sending",
    ).values(status="review"))
    await session.commit()
    try:
        recipients = list((await session.scalars(select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.status == "pending",
        ).order_by(CampaignRecipient.id).limit(min(100, max(1, batch_size))))).all())
        for recipient in recipients:
            running = await session.scalar(select(TargetedCampaign.id).where(
                TargetedCampaign.id == campaign_id, TargetedCampaign.status == "running", TargetedCampaign.lock_token == token))
            if not running:
                break
            user = await session.get(BotUser, recipient.user_id)
            pref = await session.get(MarketingPreference, (recipient.user_id, shop_id or 0))
            if not user or user.is_blocked or user.role not in {"user", "reseller"} or (pref and not pref.enabled):
                recipient.status = "skipped"
                await session.commit()
                continue
            # Recheck current membership; snapshot ids are never trusted as a tenant boundary.
            member = await session.scalar(select(BotUser.id).where(BotUser.id == user.id, _member_clause(shop_id)))
            if member is None:
                recipient.status = "skipped"
                await session.commit()
                continue
            attempted_at = datetime.now(timezone.utc)
            recipient_claim = await session.execute(update(CampaignRecipient).where(
                CampaignRecipient.id == recipient.id, CampaignRecipient.status == "pending",
                exists(select(TargetedCampaign.id).where(
                    TargetedCampaign.id == campaign_id, TargetedCampaign.status == "running",
                    TargetedCampaign.lock_token == token, TargetedCampaign.locked_until >= attempted_at,
                )),
            ).values(status="sending", attempted_at=attempted_at).execution_options(synchronize_session=False))
            await session.commit()
            if recipient_claim.rowcount != 1:
                break
            await session.refresh(recipient, attribute_names=["status", "attempted_at"])
            try:
                message = await bot.send_message(user.telegram_id, text, parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="قطع پیام‌های پیشنهادی", callback_data="campaign:optout")]]))
                recipient.status, recipient.message_id = "sent", message.message_id
            except TelegramRetryAfter as exc:
                recipient.status = "pending"
                await session.execute(update(TargetedCampaign).where(
                    TargetedCampaign.id == campaign_id, TargetedCampaign.lock_token == token,
                ).values(lock_token=None, locked_until=datetime.now(timezone.utc) + timedelta(seconds=max(1, exc.retry_after))))
                await session.commit()
                return
            except (TelegramForbiddenError, TelegramBadRequest):
                recipient.status = "failed"
            except Exception:
                recipient.status = "review"
            await session.commit()
            if delay:
                await asyncio.sleep(min(1, max(0.1, delay)))
        progress = await campaign_progress(session, campaign_id)
        if not progress.get("pending") and not progress.get("sending"):
            await session.execute(update(TargetedCampaign).where(
                TargetedCampaign.id == campaign_id, TargetedCampaign.status == "running", TargetedCampaign.lock_token == token,
            ).values(status="review" if progress.get("review") else "completed"))
            await session.commit()
    finally:
        await session.execute(update(TargetedCampaign).where(
            TargetedCampaign.id == campaign_id, TargetedCampaign.lock_token == token,
        ).values(lock_token=None, locked_until=None))
        await session.commit()
