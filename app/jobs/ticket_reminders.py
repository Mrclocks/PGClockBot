"""Send due unanswered-ticket reminders to each ticket's own shop staff."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal
from app.services.db_safe import rollback_quiet
from app.services.notifications import notify_ticket_reminder
from app.services.ticket_reminders import DueTicket, claim_reminder, due_tickets, reminder_cutoffs

logger = logging.getLogger(__name__)

# Each reminder fans out to a few staff chats; spacing keeps the bots far below Telegram's limits.
SEND_PAUSE_SECONDS = 0.5


async def run_ticket_reminders(bot: Bot) -> int:
    """Claim then send each due reminder; one failing ticket never stops the batch."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        due = await due_tickets(session, await reminder_cutoffs(session, now=now))
    sent = 0
    for item in due:
        try:
            sent += await _remind(bot, item, now=now)
        except TelegramRetryAfter as exc:
            logger.warning("ticket reminders paused by Telegram for %ss", exc.retry_after)
            await asyncio.sleep(min(float(exc.retry_after), 60.0))
        except Exception:
            logger.exception("ticket reminder failed ticket=%s", item.ticket_id)
        await asyncio.sleep(SEND_PAUSE_SECONDS)
    return sent


async def _remind(bot: Bot, item: DueTicket, *, now: datetime) -> int:
    async with SessionLocal() as session:
        try:
            if not await claim_reminder(session, item, now=now):
                return 0
        except SQLAlchemyError:
            await rollback_quiet(session)
            raise
        # The claim is committed: a crash from here on skips this reminder instead of repeating it.
        await notify_ticket_reminder(
            bot,
            session,
            ticket_id=item.ticket_id,
            user_id=item.user_id,
            shop_id=item.shop_id,
            subject=item.subject,
            waiting_seconds=int((now - item.waiting_since).total_seconds()),
        )
    return 1
