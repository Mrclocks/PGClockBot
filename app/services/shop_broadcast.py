"""Shop-scoped group messages: same queue as the owner broadcast, one shop's customers only."""

from __future__ import annotations

import html
import secrets
import string

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, BroadcastLog, MarketingPreference, ResellerProfile, ShopBroadCampaignDay
from app.services.broadcast_queue import STATUS_QUEUED
from app.services.campaigns import BROAD_PER_DAY, RECIPIENT_LIMIT, _member_clause
from app.services.daily_cap import claim_daily_cap
from app.services.rich_text import utf16_len
from app.services.shop_customer_block import not_blocked_in_shop_clause

SHOP_AUDIENCE_LABELS = {"all": "همهٔ مشتریان"}
SHOP_BROAD_LIMIT_MESSAGE = (
    "پیام گروهی گسترده برای هر فروشگاه روزی یک‌بار است (با کمپین گسترده مشترک است)"
)
SHOP_NO_BOT_MESSAGE = "ربات فروشگاه فعال نیست؛ ابتدا ربات شاپ را وصل کنید"
MAX_SHOP_TEXT_UTF16 = 4000
REQUEST_KEY_LEN = 32


def shop_audience_clause(shop_id: int):
    """Customers of this shop who may receive a group message (campaigns membership rules)."""
    opted_out = exists(
        select(MarketingPreference.user_id).where(
            MarketingPreference.user_id == BotUser.id,
            MarketingPreference.shop_key == int(shop_id),
            MarketingPreference.enabled.is_(False),
        )
    )
    return [
        _member_clause(int(shop_id)),
        BotUser.is_blocked.is_(False),
        not_blocked_in_shop_clause(int(shop_id)),
        BotUser.role.in_(["user", "reseller"]),
        ~opted_out,
    ]


def clean_shop_broadcast_text(raw: object) -> str:
    """Validate and return the raw body; escaping happens only at Telegram send time."""
    text = str(raw or "").replace("\x00", "").strip()
    if not text:
        raise ValueError("متن پیام خالی است")
    if utf16_len(text) > MAX_SHOP_TEXT_UTF16:
        raise ValueError(f"متن پیام خیلی طولانی است (حداکثر {MAX_SHOP_TEXT_UTF16})")
    # Escaped HTML must still fit Telegram's limit when the worker sends with parse_mode=HTML.
    if utf16_len(html.escape(text, quote=False)) > MAX_SHOP_TEXT_UTF16:
        raise ValueError(f"متن پیام خیلی طولانی است (حداکثر {MAX_SHOP_TEXT_UTF16})")
    return text


def shop_broadcast_html_snippet(raw: object, *, limit: int) -> str:
    """Truncate the raw body first, then escape, so HTML entities are never split."""
    snippet = str(raw or "")[: max(0, int(limit))]
    return html.escape(snippet, quote=False)


def parse_shop_request_key(raw: object, *, shop_id: int) -> str:
    """Build a globally unique request key scoped to the shop."""
    token = str(raw or "").strip()
    if len(token) != REQUEST_KEY_LEN or any(c not in string.hexdigits for c in token):
        token = secrets.token_hex(REQUEST_KEY_LEN // 2)
    return f"shop-{int(shop_id)}-{token.lower()}"


def new_shop_request_key() -> str:
    return secrets.token_hex(REQUEST_KEY_LEN // 2)


async def shop_can_send_broadcast(session: AsyncSession, *, shop_id: int) -> bool:
    """True when the shop has an active dedicated bot token (no secret is returned)."""
    profile = await session.scalar(
        select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id))
    )
    return bool(profile and profile.is_active and profile.bot_token)


async def count_shop_broadcast_targets(session: AsyncSession, *, shop_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(BotUser).where(*shop_audience_clause(shop_id))
        )
        or 0
    )


async def queue_shop_broadcast(
    session: AsyncSession,
    *,
    shop_id: int,
    text: object,
    created_by: str | None,
    request_key: object = None,
) -> BroadcastLog:
    """Enqueue one shop broadcast; daily broad cap and request_key make repeats safe."""
    if not isinstance(shop_id, int) or isinstance(shop_id, bool) or shop_id <= 0:
        raise ValueError("فروشگاه نامعتبر است")
    body = clean_shop_broadcast_text(text)
    key = parse_shop_request_key(request_key, shop_id=shop_id)
    existing = await session.scalar(select(BroadcastLog).where(BroadcastLog.request_key == key))
    if existing is not None:
        return existing
    if not await shop_can_send_broadcast(session, shop_id=shop_id):
        raise ValueError(SHOP_NO_BOT_MESSAGE)
    total = await count_shop_broadcast_targets(session, shop_id=shop_id)
    if not total:
        raise ValueError("مخاطبی برای این گروه وجود ندارد")
    total = min(total, RECIPIENT_LIMIT)
    # Cap claim and insert share one nested transaction so a colliding request_key
    # rolls the claim back instead of burning the shop's daily broad slot.
    try:
        async with session.begin_nested():
            if not await claim_daily_cap(
                session, ShopBroadCampaignDay, shop_id=int(shop_id), amount=1, cap=BROAD_PER_DAY
            ):
                raise ValueError(SHOP_BROAD_LIMIT_MESSAGE)
            log = BroadcastLog(
                audience="all",
                text=body,
                total=total,
                created_by=(created_by or "")[:128] or None,
                status=STATUS_QUEUED,
                shop_id=int(shop_id),
                request_key=key,
            )
            session.add(log)
            await session.flush()
    except IntegrityError:
        again = await session.scalar(select(BroadcastLog).where(BroadcastLog.request_key == key))
        if again is not None:
            return again
        raise
    await session.commit()
    return log
