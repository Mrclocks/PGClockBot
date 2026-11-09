"""Durable queue of customer notices: enqueue with the operation, send in small throttled steps.

A notice is claimed (``pending`` -> ``sending``, committed) before the Telegram call, so two workers
never send the same row. A worker that dies after the claim leaves the row ``sending``; the next run
moves it to ``review`` and never sends it again, because Telegram may already have accepted it.
A retryable failure goes back to ``pending`` with a later ``next_attempt_at``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CustomerNotice

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_REVIEW = "review"
OUTCOME_SENT = "sent"
OUTCOME_SKIPPED = "skipped"
OUTCOME_RETRY = "retry"
OUTCOME_FAILED = "failed"
NOTICE_BATCH_SIZE = 50
STALE_SENDING_AFTER = timedelta(minutes=5)
RETRY_BACKOFF = timedelta(minutes=1)
MAX_ATTEMPTS = 3
SERVICE_NAME_MAX = 128
REASON_MAX = 255


@dataclass(frozen=True, slots=True)
class NoticeOutcome:
    """What the sender did with one notice; ``retry_after`` overrides the default backoff."""

    kind: str
    retry_after: timedelta | None = None


Sender = Callable[[CustomerNotice], Awaitable[NoticeOutcome]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def add_notice(
    session: AsyncSession,
    *,
    shop_id: int | None,
    bot_user_id: int,
    telegram_id: int,
    action: str,
    service_name: str,
    reason: str,
    service_id: int | None = None,
) -> CustomerNotice:
    """Add one pending notice to the session; the caller's commit stores it."""
    row = CustomerNotice(
        shop_id=shop_id,
        bot_user_id=int(bot_user_id),
        telegram_id=int(telegram_id),
        service_id=int(service_id) if service_id else None,
        action=action,
        service_name=(service_name or "")[:SERVICE_NAME_MAX],
        reason=reason[:REASON_MAX],
        status=STATUS_PENDING,
    )
    session.add(row)
    return row


async def _expire_stale(session: AsyncSession, now: datetime) -> None:
    await session.execute(
        update(CustomerNotice)
        .where(CustomerNotice.status == STATUS_SENDING, CustomerNotice.attempted_at < now - STALE_SENDING_AFTER)
        .values(status=STATUS_REVIEW),
        execution_options={"synchronize_session": False},
    )
    await session.commit()


async def _due(session: AsyncSession, now: datetime, limit: int) -> list[CustomerNotice]:
    return list(
        (
            await session.scalars(
                select(CustomerNotice)
                .where(
                    CustomerNotice.status == STATUS_PENDING,
                    or_(CustomerNotice.next_attempt_at.is_(None), CustomerNotice.next_attempt_at <= now),
                )
                .order_by(CustomerNotice.id)
                .limit(limit)
            )
        ).all()
    )


async def _claim(session: AsyncSession, notice_id: int, now: datetime) -> bool:
    result = await session.execute(
        update(CustomerNotice)
        .where(CustomerNotice.id == notice_id, CustomerNotice.status == STATUS_PENDING)
        .values(status=STATUS_SENDING, attempted_at=now, attempts=CustomerNotice.attempts + 1),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    return result.rowcount == 1


async def _settle(session: AsyncSession, notice: CustomerNotice, outcome: NoticeOutcome, now: datetime) -> None:
    status, next_at = {
        OUTCOME_SENT: (STATUS_SENT, None),
        OUTCOME_SKIPPED: (STATUS_SKIPPED, None),
    }.get(outcome.kind, (STATUS_FAILED, None))
    if outcome.kind == OUTCOME_RETRY:
        exhausted = notice.attempts >= MAX_ATTEMPTS
        status = STATUS_FAILED if exhausted else STATUS_PENDING
        next_at = None if exhausted else now + (outcome.retry_after or RETRY_BACKOFF)
    await session.execute(
        update(CustomerNotice)
        .where(CustomerNotice.id == notice.id, CustomerNotice.status == STATUS_SENDING)
        .values(status=status, next_attempt_at=next_at),
        execution_options={"synchronize_session": False},
    )
    await session.commit()


async def process_notice_batch(
    session: AsyncSession, send: Sender, *, limit: int = NOTICE_BATCH_SIZE, now: datetime | None = None
) -> int:
    """Send one bounded batch; returns how many notices were handed to ``send``.

    A failing sender is logged and leaves that notice ``sending`` (then ``review``): an unknown
    outcome is never retried blindly.
    """
    now = now or _utcnow()
    await _expire_stale(session, now)
    handled = 0
    for notice in await _due(session, now, limit):
        if not await _claim(session, int(notice.id), now):
            continue
        await session.refresh(notice)
        try:
            outcome = await send(notice)
        except Exception:  # noqa: BLE001 - one bad notice must not stop the batch; its outcome is unknown
            logger.exception("customer notice send raised id=%s", notice.id)
            continue
        handled += 1
        await _settle(session, notice, outcome, now)
    return handled
