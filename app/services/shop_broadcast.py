"""Shop-scoped group messages: same queue as the owner broadcast, one shop's customers only."""

from __future__ import annotations

import html
import secrets
import string
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BroadcastLog, ResellerProfile
from app.services.broadcast_audience import (
    AudienceSpec,
    audience_clause,
    checked_spec,
    count_audience,
    is_broad,
    make_spec,
)
from app.services.broadcast_media import CAPTION_TOO_LONG, BroadcastMedia, text_limit
from app.services.broadcast_queue import STATUS_QUEUED, media_columns, scheduled_spec
from app.services.broadcast_schedule import checked_send_after
from app.services.campaigns import RECIPIENT_LIMIT, claim_shop_message_slot
from app.services.rich_text import utf16_len

SHOP_BROAD_LIMIT_MESSAGE = (
    "پیام گروهی گسترده برای هر فروشگاه روزی یک‌بار است (با کمپین گسترده مشترک است)"
)
SHOP_NO_BOT_MESSAGE = "ربات فروشگاه فعال نیست؛ ابتدا ربات شاپ را وصل کنید"
MAX_SHOP_TEXT_UTF16 = 4000
REQUEST_KEY_LEN = 32


def shop_audience_clause(shop_id: int, spec: AudienceSpec | None = None):
    """Customers of this shop who may receive a group message: ``spec`` (all customers when None)."""
    return audience_clause(spec or make_spec("all", shop_id=int(shop_id)), shop_id=int(shop_id))


def clean_shop_broadcast_text(raw: object, *, with_media: bool = False) -> str:
    """Validate and return the raw body; escaping happens only at Telegram send time.

    With media the body is the caption: it may be empty and is limited to Telegram's caption length.
    """
    text = str(raw or "").replace("\x00", "").strip()
    if not text and not with_media:
        raise ValueError("متن پیام خالی است")
    limit = text_limit(with_media=with_media, default=MAX_SHOP_TEXT_UTF16)
    too_long = CAPTION_TOO_LONG if with_media else f"متن پیام خیلی طولانی است (حداکثر {MAX_SHOP_TEXT_UTF16})"
    # Escaped HTML must still fit Telegram's limit when the worker sends with parse_mode=HTML.
    if utf16_len(text) > limit or utf16_len(html.escape(text, quote=False)) > limit:
        raise ValueError(too_long)
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


async def count_shop_broadcast_targets(session: AsyncSession, *, shop_id: int, spec: AudienceSpec | None = None) -> int:
    return await count_audience(session, spec or make_spec("all", shop_id=int(shop_id)), shop_id=int(shop_id))


async def queue_shop_broadcast(
    session: AsyncSession,
    *,
    shop_id: int,
    text: object,
    created_by: str | None,
    request_key: object = None,
    buttons: str | None = None,
    audience: AudienceSpec | None = None,
    send_after: datetime | None = None,
    media: BroadcastMedia | None = None,
) -> BroadcastLog:
    """Enqueue one shop broadcast; the daily broad cap and request_key make repeats safe.

    A message to (nearly) all customers (``is_broad``) uses the shop's one broad slot of the day; a narrower segment
    uses one of the shop's daily targeted slots (``TARGETED_PER_DAY``). Both are capped at ``RECIPIENT_LIMIT``.
    """
    if not isinstance(shop_id, int) or isinstance(shop_id, bool) or shop_id <= 0:
        raise ValueError("فروشگاه نامعتبر است")
    body = clean_shop_broadcast_text(text, with_media=media is not None)
    key = parse_shop_request_key(request_key, shop_id=shop_id)
    existing = await session.scalar(select(BroadcastLog).where(BroadcastLog.request_key == key))
    if existing is not None:
        return existing
    if not await shop_can_send_broadcast(session, shop_id=shop_id):
        raise ValueError(SHOP_NO_BOT_MESSAGE)
    send_after = checked_send_after(send_after)
    spec = make_spec("all", shop_id=shop_id) if audience is None else audience
    spec = await checked_spec(session, scheduled_spec(spec, send_after), shop_id=shop_id)
    total = await count_shop_broadcast_targets(session, shop_id=shop_id, spec=spec)
    if not total:
        raise ValueError("مخاطبی برای این گروه وجود ندارد")
    total = min(total, RECIPIENT_LIMIT)
    # Cap claim and insert share one nested transaction so a colliding request_key
    # rolls the claim back instead of burning one of the shop's daily slots.
    try:
        async with session.begin_nested():
            await claim_shop_message_slot(
                session, shop_id=int(shop_id), broad=is_broad(spec), broad_message=SHOP_BROAD_LIMIT_MESSAGE
            )
            log = BroadcastLog(
                audience=spec.segment,
                audience_spec=spec.to_json(),
                text=body,
                total=total,
                created_by=(created_by or "")[:128] or None,
                status=STATUS_QUEUED,
                shop_id=int(shop_id),
                request_key=key,
                buttons=buttons,
                send_after=send_after,
                **media_columns(media),
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
