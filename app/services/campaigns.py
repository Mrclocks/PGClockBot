"""Shop-scoped audience snapshots and resumable Telegram campaigns."""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import String, and_, cast, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import BotUser, CampaignRecipient, MarketingPreference, Order, Plan, TargetedCampaign, UserService

AUDIENCES = {"trial": "کاربران تست بدون خرید", "expiring": "سرویس‌های نزدیک انقضا", "inactive": "مشتریان بدون خرید در بازهٔ انتخابی"}
RECIPIENT_LIMIT = 5000


def audience_query(*, shop_id, audience, days, now=None):
    if audience not in AUDIENCES or not 1 <= days <= 365:
        raise ValueError("گروه مخاطبان یا تعداد روز نامعتبر است")
    now = now or datetime.now(timezone.utc)
    shop_order = and_(Order.user_id == BotUser.id, Order.reseller_id == shop_id)
    bought = exists(select(Order.id).where(shop_order, Order.status == "delivered", Order.amount > 0))
    member = or_(BotUser.reseller_id == shop_id, exists(select(Order.id).where(shop_order)))
    opted_out = exists(select(MarketingPreference.user_id).where(
        MarketingPreference.user_id == BotUser.id, MarketingPreference.shop_key == (shop_id or 0), MarketingPreference.enabled.is_(False)))
    query = select(BotUser.id).where(member, BotUser.is_blocked.is_(False), BotUser.role.in_(["user", "reseller"]), ~opted_out)
    if audience == "trial":
        query = query.where(~bought, exists(select(Order.id).join(Plan, Plan.id == Order.plan_id).where(
            shop_order, Order.status == "delivered", Plan.is_trial.is_(True))))
    elif audience == "inactive":
        cutoff = now - timedelta(days=days)
        query = query.where(BotUser.created_at <= cutoff, ~exists(select(Order.id).where(
            shop_order, Order.status == "delivered", Order.created_at > cutoff)))
    else:
        origin = exists(select(Order.id).where(
            Order.user_id == UserService.bot_user_id, Order.reseller_id == shop_id,
            UserService.remark == ("order:" + cast(Order.id, String))))
        legacy = and_(or_(UserService.remark.is_(None), ~UserService.remark.like("order:%")), BotUser.reseller_id == shop_id)
        query = query.where(exists(select(UserService.id).where(
            UserService.bot_user_id == BotUser.id, or_(origin, legacy),
            UserService.is_cancelled.is_(False), UserService.cancellation_pending.is_(False),
            UserService.quota_status == "active",
            UserService.quota_synced_at >= now - timedelta(days=1),
            UserService.quota_expire_at >= now, UserService.quota_expire_at <= now + timedelta(days=days))))
    return query.order_by(BotUser.id)


async def audience_count(session, **kwargs):
    return int(await session.scalar(select(func.count()).select_from(audience_query(**kwargs).subquery())) or 0)


async def create_campaign(session, *, shop_id, audience, days, text, actor):
    text = str(text or "").replace("\x00", "").strip()
    if not text or len(text) > 3500:
        raise ValueError("متن پیام باید بین ۱ تا ۳۵۰۰ نویسه باشد")
    # Plain Telegram text only (parse_mode=None). Reject HTML-ish payloads that
    # would confuse operators if ever shown elsewhere with richer rendering.
    if "<script" in text.lower() or "javascript:" in text.lower():
        raise ValueError("متن پیام نامعتبر است")
    ids = list((await session.scalars(audience_query(shop_id=shop_id, audience=audience, days=days).limit(RECIPIENT_LIMIT))).all())
    if not ids:
        raise ValueError("مخاطبی در این گروه موجود نیست")
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


async def campaign_progress(session, campaign_id):
    return dict((await session.execute(select(CampaignRecipient.status, func.count()).where(
        CampaignRecipient.campaign_id == campaign_id).group_by(CampaignRecipient.status))).all())


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
            member = await session.scalar(select(BotUser.id).where(
                BotUser.id == user.id, or_(BotUser.reseller_id == shop_id, exists(select(Order.id).where(
                    Order.user_id == BotUser.id, Order.reseller_id == shop_id)))))
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
