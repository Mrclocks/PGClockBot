"""Claim durable cancellation notices before dispatching them outside transactions."""
from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ServiceCancellation, UserService
from app.services.formatting import format_user_label

logger = logging.getLogger(__name__)
NOTIFY_KEY = "notify_service_cancellation"
NOTICE_BATCH_SIZE = 20
NoticeOutcome = Literal["sent", "skipped", "pending", "review"]


@dataclass(frozen=True, slots=True)
class CancellationNotice:
    request_id: int
    shop_id: int | None
    text: str


def _notice(row: ServiceCancellation, user: BotUser, username: str) -> CancellationNotice:
    return CancellationNotice(row.id, row.reseller_id, (
        "📝 <b>درخواست لغو سرویس</b>\n\n"
        f"درخواست: #{row.id}\n"
        f"کاربر: {format_user_label(user)}\n"
        f"شناسه تلگرام: <code>{user.telegram_id}</code>\n"
        f"سرویس: <code>{html.escape(username)}</code> (#{row.service_id})\n"
        f"دلیل لغو:\n{html.escape(row.reason)}\n\n"
        "برای بررسی و تعیین اعتبار برگشتی، مدیریت مالی ← درخواست‌های لغو را باز کنید."
    ))


async def process_cancellation_notifications(
    session: AsyncSession, send: Callable[[CancellationNotice], Awaitable[NoticeOutcome]],
) -> None:
    now = datetime.now(timezone.utc)
    # A process death after Telegram accepted a message has an unknown outcome.
    await session.execute(update(ServiceCancellation).where(
        ServiceCancellation.notification_status == "sending",
        ServiceCancellation.notification_attempted_at < now - timedelta(minutes=5),
    ).values(notification_status="review"))
    rows = (await session.execute(select(ServiceCancellation, BotUser, UserService.pg_username).join(
        BotUser, BotUser.id == ServiceCancellation.user_id,
    ).join(UserService, (UserService.id == ServiceCancellation.service_id) & (
        UserService.bot_user_id == ServiceCancellation.user_id
    )).where(
        ServiceCancellation.notification_status == "pending",
        or_(ServiceCancellation.notification_attempted_at.is_(None),
            ServiceCancellation.notification_attempted_at < now - timedelta(minutes=1)),
    ).order_by(ServiceCancellation.id).limit(NOTICE_BATCH_SIZE))).all()
    notices = [_notice(row, user, username) for row, user, username in rows]
    await session.commit()
    for notice in notices:
        try:
            await _process_notice(session, notice, send)
        except Exception:
            await session.rollback()
            logger.exception("cancellation notice processing failed request=%s", notice.request_id)


async def _process_notice(
    session: AsyncSession, notice: CancellationNotice,
    send: Callable[[CancellationNotice], Awaitable[NoticeOutcome]],
) -> None:
    claim = await session.execute(update(ServiceCancellation).where(
        ServiceCancellation.id == notice.request_id,
        ServiceCancellation.reseller_id == notice.shop_id,
        ServiceCancellation.notification_status == "pending",
    ).values(notification_status="sending", notification_attempted_at=datetime.now(timezone.utc)))
    await session.commit()
    if claim.rowcount != 1:
        return
    try:
        outcome = await send(notice)
    except Exception:
        await session.rollback()
        outcome = "review"
        logger.exception("cancellation notice needs review request=%s", notice.request_id)
    await session.execute(update(ServiceCancellation).where(
        ServiceCancellation.id == notice.request_id,
        ServiceCancellation.reseller_id == notice.shop_id,
        ServiceCancellation.notification_status == "sending",
    ).values(notification_status=outcome))
    await session.commit()
