"""Durable owner broadcasts: queued once, sent in small throttled steps by a background worker.

A broadcast row keeps its progress (a keyset cursor and counts), a lease and the user a message is
being sent to. A worker that dies mid-send leaves that user marked in flight; the next worker counts
the outcome as unknown and moves past the user, so nobody is messaged twice. Only platform customers
are reached: a shop's customers belong to the shop's own bot.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, BroadcastLog, Role
from app.services.broadcast import AUDIENCE_LABELS
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
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
STATUS_LABELS = {
    STATUS_QUEUED: "در صف ارسال",
    STATUS_RUNNING: "در حال ارسال",
    STATUS_DONE: "ارسال شد",
    STATUS_CANCELLED: "لغو شد",
}
OUTCOME_OK, OUTCOME_FAILED, OUTCOME_UNKNOWN = "ok", "failed", "unknown"


def _audience_clause(audience: str):
    """Recipients of the platform bot for one audience."""
    clause = [BotUser.is_blocked.is_(False), shop_member_clause(None)]
    if audience == "users":
        clause.append(BotUser.role.in_([Role.USER.value, Role.RESELLER.value]))
    elif audience == "resellers":
        clause.append(BotUser.role == Role.RESELLER.value)
    elif audience == "admins":
        clause.append(BotUser.role == Role.ADMIN.value)
    return clause


async def queue_broadcast(session: AsyncSession, *, text: str, audience: str, created_by: str | None) -> BroadcastLog:
    """Record the broadcast and its audience size; the worker sends it."""
    body = (text or "").strip()
    if not body:
        raise ValueError("متن پیام خالی است")
    if len(body) > MAX_TEXT_LENGTH:
        raise ValueError(f"متن پیام خیلی طولانی است (حداکثر {MAX_TEXT_LENGTH})")
    if audience not in AUDIENCE_LABELS:
        raise ValueError("گروه مخاطبان نامعتبر است")
    total = int(await session.scalar(select(func.count()).select_from(BotUser).where(*_audience_clause(audience))) or 0)
    if not total:
        raise ValueError("مخاطبی برای این گروه وجود ندارد")
    log = BroadcastLog(audience=audience, text=body, total=total, created_by=(created_by or "")[:128] or None, status=STATUS_QUEUED)
    session.add(log)
    await session.commit()
    return log


async def cancel_broadcast(session: AsyncSession, log_id: int) -> bool:
    """Stop a queued or running broadcast; messages already sent stay sent."""
    changed = await session.execute(
        update(BroadcastLog)
        .where(BroadcastLog.id == int(log_id), BroadcastLog.status.in_(ACTIVE_STATUSES))
        .values(status=STATUS_CANCELLED, completed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return changed.rowcount == 1


async def active_broadcast_ids(session: AsyncSession, *, limit: int = 5) -> list[int]:
    now = datetime.now(timezone.utc)
    rows = await session.scalars(
        select(BroadcastLog.id)
        .where(BroadcastLog.status.in_(ACTIVE_STATUSES), or_(BroadcastLog.locked_until.is_(None), BroadcastLog.locked_until < now))
        .order_by(BroadcastLog.id)
        .limit(max(1, min(int(limit), 20)))
    )
    return [int(log_id) for log_id in rows]


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


async def _send(bot: Bot, telegram_id: int, text: str) -> str:
    try:
        await bot.send_message(telegram_id, text, parse_mode="HTML")
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
        row = (await session.execute(select(BroadcastLog.audience, BroadcastLog.text, BroadcastLog.cursor_id).where(BroadcastLog.id == log_id))).one()
        audience, text, cursor = row
        recipients = (
            await session.execute(
                select(BotUser.id, BotUser.telegram_id)
                .where(*_audience_clause(audience), BotUser.id > int(cursor))
                .order_by(BotUser.id)
                .limit(max(1, min(int(batch_size), 500)))
            )
        ).all()
        for user_id, telegram_id in recipients:
            if not await _mark_inflight(session, log_id, token, int(user_id)):
                return False
            try:
                outcome = await _send(bot, telegram_id, text)
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
        if len(recipients) < batch_size:
            await _finish(session, log_id, token)
            keep_going = False
        return keep_going
    finally:
        await _release(session, log_id, token)


async def run_broadcast_worker(session_factory, bot: Bot, *, budget_seconds: float = TICK_BUDGET_SECONDS) -> None:
    """One scheduler tick: advance each active broadcast until the time budget is used."""
    async with session_factory() as session:
        log_ids = await active_broadcast_ids(session)
    deadline = time.monotonic() + budget_seconds
    for log_id in log_ids:
        while time.monotonic() < deadline:
            async with session_factory() as session:
                try:
                    more = await process_broadcast_batch(session, log_id, bot)
                except Exception:
                    logger.exception("broadcast batch failed id=%s", log_id)
                    break
            if not more:
                break
