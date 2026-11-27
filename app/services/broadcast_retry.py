"""Resend a finished group message to the recipients it did not reach.

A resend is a new queued broadcast with the same text, buttons and audience, narrowed to people with a
``broadcast_failures`` row of the original (``retry_of_id``); the worker sends it like any other broadcast. People
still marked unreachable stay out, so in practice it reaches those whose message was rejected or who started the bot
again. Unknown outcomes are left out unless the sender asks, because those people may already have the message.
A broadcast is resent at most once (a unique index), so a repeated submit returns the same resend. A shop's resend
does not use one of its daily message slots: it repeats a message the shop already sent, to a subset of its audience.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, BroadcastLog
from app.services.broadcast import shop_scope_filter
from app.services.broadcast_audience import StoredAudienceError, load_spec
from app.services.broadcast_queue import ACTIVE_STATUSES, STATUS_QUEUED, RetrySource, recipient_clauses
from app.services.shop_broadcast import SHOP_NO_BOT_MESSAGE, shop_can_send_broadcast

RETRY_NOTHING_LEFT = "کسی برای ارسال دوباره نمانده است"
RETRY_NOT_FINISHED = "ارسال این پیام هنوز تمام نشده است"
RETRY_NOT_FOUND = "این پیام پیدا نشد"


@dataclass(frozen=True, slots=True)
class RetryOffer:
    """Who a resend of a finished broadcast would reach now, or the resend already queued."""

    failed: int
    with_unknown: int
    existing_id: int | None


async def _count(session: AsyncSession, log: BroadcastLog, *, include_unknown: bool) -> int:
    tenant = int(log.shop_id) if log.shop_id is not None else None
    try:
        spec = load_spec(log.audience_spec, log.audience, shop_id=tenant)
    except StoredAudienceError as exc:
        raise ValueError("مخاطبان این پیام خوانده نمی‌شود") from exc
    clauses = recipient_clauses(spec, shop_id=tenant, retry=RetrySource(int(log.id), include_unknown))
    return int(await session.scalar(select(func.count()).select_from(BotUser).where(*clauses)) or 0)


async def _existing_retry(session: AsyncSession, log_id: int) -> BroadcastLog | None:
    return await session.scalar(select(BroadcastLog).where(BroadcastLog.retry_of_id == int(log_id)))


async def retry_offer(session: AsyncSession, log: BroadcastLog) -> RetryOffer | None:
    """None while ``log`` is still sending or its audience cannot be read."""
    if log.status in ACTIVE_STATUSES:
        return None
    existing = await _existing_retry(session, log.id)
    if existing is not None:
        return RetryOffer(0, 0, int(existing.id))
    try:
        failed = await _count(session, log, include_unknown=False)
        with_unknown = await _count(session, log, include_unknown=True) if log.review_count else failed
    except ValueError:
        return None
    return RetryOffer(failed, with_unknown, None)


async def queue_retry(
    session: AsyncSession, log_id: int, *, shop_id: int | None, include_unknown: bool, created_by: str | None
) -> BroadcastLog:
    """Queue the one resend of the tenant's finished broadcast ``log_id``; a repeat returns that resend."""
    log = await session.scalar(
        select(BroadcastLog)
        .where(BroadcastLog.id == int(log_id), shop_scope_filter(shop_id=shop_id))
        # The worker changes the status in its own session; read the current row, not a cached one.
        .execution_options(populate_existing=True)
    )
    if log is None:
        raise ValueError(RETRY_NOT_FOUND)
    if log.status in ACTIVE_STATUSES:
        raise ValueError(RETRY_NOT_FINISHED)
    existing = await _existing_retry(session, log.id)
    if existing is not None:
        return existing
    if shop_id is not None and not await shop_can_send_broadcast(session, shop_id=int(shop_id)):
        raise ValueError(SHOP_NO_BOT_MESSAGE)
    total = await _count(session, log, include_unknown=include_unknown)
    if not total:
        raise ValueError(RETRY_NOTHING_LEFT)
    resend = BroadcastLog(
        audience=log.audience,
        audience_spec=log.audience_spec,
        text=log.text,
        buttons=log.buttons,
        total=total,
        created_by=(created_by or "")[:128] or None,
        status=STATUS_QUEUED,
        shop_id=log.shop_id,
        retry_of_id=log.id,
        retry_unknown=include_unknown,
        media_kind=log.media_kind,
        media_file_id=log.media_file_id,
        media_file=log.media_file,
    )
    session.add(resend)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent submit queued the resend first; return it.
        await session.rollback()
        again = await _existing_retry(session, int(log_id))
        if again is None:
            raise
        return again
    return resend
