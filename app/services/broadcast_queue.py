"""Durable broadcasts: queued once, sent in small throttled steps by a background worker.

A broadcast row keeps its progress (a keyset cursor and counts), a lease and the user a message is
being sent to. A worker that dies mid-send leaves that user marked in flight; the next worker counts
the outcome as unknown and moves past the user, so nobody is messaged twice. Platform rows
(``shop_id IS NULL``) use the platform bot; shop rows use that shop's bot and that shop's customers.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, BroadcastLog, Role
from app.services.broadcast import AUDIENCE_LABELS, shop_scope_filter
from app.services.shop_membership import shop_member_clause

logger = logging.getLogger("pgclock.broadcast")

MAX_TEXT_LENGTH = 4000
BATCH_SIZE = 100
SEND_DELAY_SECONDS = 0.05
LEASE_MINUTES = 5
TICK_BUDGET_SECONDS = 45
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
STATUS_LABELS = {
    STATUS_QUEUED: "در صف ارسال",
    STATUS_RUNNING: "در حال ارسال",
    STATUS_DONE: "ارسال شد",
    STATUS_CANCELLED: "لغو شد",
    STATUS_FAILED: "ناموفق",
}
OUTCOME_OK, OUTCOME_FAILED, OUTCOME_UNKNOWN = "ok", "failed", "unknown"


def _audience_clause(audience: str, *, shop_id: int | None = None):
    """Recipients for one broadcast: platform bot audience, or one shop's customers."""
    if shop_id is not None:
        # Lazy import: shop_broadcast imports STATUS_QUEUED from this module at load time.
        from app.services.shop_broadcast import shop_audience_clause

        return shop_audience_clause(int(shop_id))
    clause = [BotUser.is_blocked.is_(False), shop_member_clause(None)]
    if audience == "users":
        clause.append(BotUser.role.in_([Role.USER.value, Role.RESELLER.value]))
    elif audience == "resellers":
        clause.append(BotUser.role == Role.RESELLER.value)
    elif audience == "admins":
        clause.append(BotUser.role == Role.ADMIN.value)
    return clause


async def queue_broadcast(
    session: AsyncSession,
    *,
    text: str,
    audience: str,
    created_by: str | None,
) -> BroadcastLog:
    """Record a platform broadcast and its audience size; the worker sends it."""
    body = (text or "").strip()
    if not body:
        raise ValueError("متن پیام خالی است")
    if len(body) > MAX_TEXT_LENGTH:
        raise ValueError(f"متن پیام خیلی طولانی است (حداکثر {MAX_TEXT_LENGTH})")
    if audience not in AUDIENCE_LABELS:
        raise ValueError("گروه مخاطبان نامعتبر است")
    total = int(
        await session.scalar(
            select(func.count()).select_from(BotUser).where(*_audience_clause(audience))
        )
        or 0
    )
    if not total:
        raise ValueError("مخاطبی برای این گروه وجود ندارد")
    log = BroadcastLog(
        audience=audience,
        text=body,
        total=total,
        created_by=(created_by or "")[:128] or None,
        status=STATUS_QUEUED,
        shop_id=None,
    )
    session.add(log)
    await session.commit()
    return log


async def cancel_broadcast(
    session: AsyncSession,
    log_id: int,
    *,
    shop_id: int | None = None,
) -> bool:
    """Stop a queued or running broadcast in one tenant; messages already sent stay sent."""
    changed = await session.execute(
        update(BroadcastLog)
        .where(
            BroadcastLog.id == int(log_id),
            shop_scope_filter(shop_id=shop_id),
            BroadcastLog.status.in_(ACTIVE_STATUSES),
        )
        .values(status=STATUS_CANCELLED, completed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return changed.rowcount == 1


async def mark_broadcast_failed(
    session: AsyncSession,
    log_id: int,
    *,
    shop_id: int | None = None,
) -> bool:
    """End an active broadcast that cannot run (for example a shop with no bot)."""
    changed = await session.execute(
        update(BroadcastLog)
        .where(
            BroadcastLog.id == int(log_id),
            shop_scope_filter(shop_id=shop_id),
            BroadcastLog.status.in_(ACTIVE_STATUSES),
        )
        .values(
            status=STATUS_FAILED,
            completed_at=datetime.now(timezone.utc),
            lock_token=None,
            locked_until=None,
            inflight_id=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return changed.rowcount == 1


async def active_broadcast_ids(session: AsyncSession, *, limit: int = 5) -> list[int]:
    rows = await active_broadcast_jobs(session, limit=limit)
    return [log_id for log_id, _shop_id in rows]


async def active_broadcast_jobs(
    session: AsyncSession, *, limit: int = 5
) -> list[tuple[int, int | None]]:
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(BroadcastLog.id, BroadcastLog.shop_id)
            .where(
                BroadcastLog.status.in_(ACTIVE_STATUSES),
                or_(BroadcastLog.locked_until.is_(None), BroadcastLog.locked_until < now),
            )
            .order_by(BroadcastLog.id)
            .limit(max(1, min(int(limit), 20)))
        )
    ).all()
    return [(int(log_id), int(shop_id) if shop_id is not None else None) for log_id, shop_id in rows]


async def _take_lease(session: AsyncSession, log_id: int) -> str | None:
    now = datetime.now(timezone.utc)
    token = secrets.token_hex(16)
    claim = await session.execute(
        update(BroadcastLog)
        .where(
            BroadcastLog.id == log_id,
            BroadcastLog.status.in_(ACTIVE_STATUSES),
            or_(BroadcastLog.locked_until.is_(None), BroadcastLog.locked_until < now),
        )
        .values(status=STATUS_RUNNING, lock_token=token, locked_until=now + timedelta(minutes=LEASE_MINUTES))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return token if claim.rowcount == 1 else None


def _owned(log_id: int, token: str):
    now = datetime.now(timezone.utc)
    return (
        BroadcastLog.id == log_id,
        BroadcastLog.lock_token == token,
        BroadcastLog.status == STATUS_RUNNING,
        BroadcastLog.locked_until >= now,
    )


async def _settle_crashed_send(session: AsyncSession, log_id: int, token: str) -> None:
    """A user left in flight by a dead worker has an unknown outcome: count it and move past it."""
    await session.execute(
        update(BroadcastLog)
        .where(*_owned(log_id, token), BroadcastLog.inflight_id.is_not(None))
        .values(cursor_id=BroadcastLog.inflight_id, inflight_id=None, review_count=BroadcastLog.review_count + 1)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _mark_inflight(session: AsyncSession, log_id: int, token: str, user_id: int) -> bool:
    """Persist the target before the network call; False when the lease was lost or cancelled."""
    marked = await session.execute(
        update(BroadcastLog).where(*_owned(log_id, token)).values(inflight_id=user_id).execution_options(synchronize_session=False)
    )
    await session.commit()
    return marked.rowcount == 1


async def _record(session: AsyncSession, log_id: int, token: str, user_id: int, outcome: str) -> None:
    counts = {
        OUTCOME_OK: {"ok_count": BroadcastLog.ok_count + 1},
        OUTCOME_FAILED: {"fail_count": BroadcastLog.fail_count + 1},
        OUTCOME_UNKNOWN: {"review_count": BroadcastLog.review_count + 1},
    }[outcome]
    await session.execute(
        update(BroadcastLog).where(*_owned(log_id, token)).values(cursor_id=user_id, inflight_id=None, **counts)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _send(bot: Bot, telegram_id: int, text: str, *, escape_html: bool = False) -> str:
    body = html.escape(text, quote=False) if escape_html else text
    try:
        await bot.send_message(telegram_id, body, parse_mode="HTML")
    except (TelegramForbiddenError, TelegramBadRequest):
        return OUTCOME_FAILED
    return OUTCOME_OK


async def _back_off(session: AsyncSession, log_id: int, token: str, seconds: int) -> None:
    """Telegram asked to wait; nothing was delivered, so the user is retried after the pause."""
    await session.execute(
        update(BroadcastLog).where(BroadcastLog.id == log_id, BroadcastLog.lock_token == token)
        .values(inflight_id=None, lock_token=None, locked_until=datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds)))
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _finish(session: AsyncSession, log_id: int, token: str) -> None:
    await session.execute(
        update(BroadcastLog).where(*_owned(log_id, token))
        .values(status=STATUS_DONE, completed_at=datetime.now(timezone.utc), inflight_id=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _release(session: AsyncSession, log_id: int, token: str) -> None:
    await session.execute(
        update(BroadcastLog).where(BroadcastLog.id == log_id, BroadcastLog.lock_token == token)
        .values(lock_token=None, locked_until=None).execution_options(synchronize_session=False)
    )
    await session.commit()


async def process_broadcast_batch(
    session: AsyncSession, log_id: int, bot: Bot, *, batch_size: int = BATCH_SIZE, delay: float = SEND_DELAY_SECONDS
) -> bool:
    """Send the next batch. Returns True while the broadcast may have more to send."""
    token = await _take_lease(session, int(log_id))
    if token is None:
        return False
    keep_going = True
    try:
        await _settle_crashed_send(session, log_id, token)
        row = (
            await session.execute(
                select(
                    BroadcastLog.audience,
                    BroadcastLog.text,
                    BroadcastLog.cursor_id,
                    BroadcastLog.shop_id,
                    BroadcastLog.total,
                    BroadcastLog.ok_count,
                    BroadcastLog.fail_count,
                    BroadcastLog.review_count,
                ).where(BroadcastLog.id == log_id)
            )
        ).one()
        audience, text, cursor, shop_id, total, ok_count, fail_count, review_count = row
        progress = int(ok_count) + int(fail_count) + int(review_count)
        if progress >= int(total):
            await _finish(session, log_id, token)
            return False
        take = max(1, min(int(batch_size), 500, int(total) - progress))
        recipients = (
            await session.execute(
                select(BotUser.id, BotUser.telegram_id)
                .where(
                    *_audience_clause(audience, shop_id=int(shop_id) if shop_id is not None else None),
                    BotUser.id > int(cursor),
                )
                .order_by(BotUser.id)
                .limit(take)
            )
        ).all()
        escape_html = shop_id is not None
        for user_id, telegram_id in recipients:
            if not await _mark_inflight(session, log_id, token, int(user_id)):
                return False
            try:
                outcome = await _send(bot, telegram_id, text, escape_html=escape_html)
            except TelegramRetryAfter as exc:
                await _back_off(session, log_id, token, exc.retry_after)
                return True
            except Exception:
                # The message may or may not have been delivered; never send it again blindly.
                logger.warning("broadcast %s: unknown outcome for user %s", log_id, user_id, exc_info=True)
                outcome = OUTCOME_UNKNOWN
            await _record(session, log_id, token, int(user_id), outcome)
            if delay:
                await asyncio.sleep(delay)
        if len(recipients) < take:
            await _finish(session, log_id, token)
            keep_going = False
        return keep_going
    finally:
        await _release(session, log_id, token)


async def run_broadcast_worker(session_factory, bot: Bot, *, budget_seconds: float = TICK_BUDGET_SECONDS) -> None:
    """One scheduler tick: advance each active broadcast until the time budget is used."""
    async with session_factory() as session:
        jobs = await active_broadcast_jobs(session)
    deadline = time.monotonic() + budget_seconds
    for log_id, shop_id in jobs:
        send_bot, should_close = bot, False
        try:
            if shop_id is not None:
                # Lazy import: reseller_bots pulls secret/token helpers this module does not need at import.
                from app.services.reseller_bots import open_notify_bot_for_reseller

                async with session_factory() as session:
                    send_bot, should_close = await open_notify_bot_for_reseller(session, shop_id)
                if send_bot is None:
                    logger.warning(
                        "broadcast %s: shop %s has no usable bot; marking failed",
                        log_id,
                        shop_id,
                    )
                    async with session_factory() as session:
                        await mark_broadcast_failed(session, log_id, shop_id=shop_id)
                    continue
            while time.monotonic() < deadline:
                async with session_factory() as session:
                    try:
                        more = await process_broadcast_batch(session, log_id, send_bot)
                    except Exception:
                        logger.exception("broadcast batch failed id=%s", log_id)
                        break
                if not more:
                    break
        finally:
            if should_close and send_bot is not None:
                await send_bot.session.close()
