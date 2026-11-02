"""Staff notices of a ticket replace each other and go away once the ticket is closed.

Every «🎫 تیکت جدید» / «🎫 پیام جدید تیکت» notice of one ticket shares a replace key, so a staff
chat shows only the latest notice of each open ticket. Closing the ticket, from the bot or the
web panel, retires that last notice in every staff chat of the bot that sent it.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Ticket
from app.services.replaceable_notices import clear_notices
from app.services.reseller_bots import open_notify_bot_for_reseller

logger = logging.getLogger(__name__)

TICKET_CLOSED_TEXT = "✅ این تیکت بسته شد."


def ticket_notice_key(ticket_id: int) -> str:
    return f"ticket:{int(ticket_id)}"


async def _notice_bot(session: AsyncSession, reseller_id: int | None) -> tuple[Bot | None, bool]:
    """The bot that sent this ticket's staff notices, and whether it must be closed afterwards."""
    if reseller_id:
        return await open_notify_bot_for_reseller(session, int(reseller_id))
    # Breaks the import cycle app.bot -> handlers -> services.
    from app.bot import create_bot

    token = (get_settings().bot_token or "").strip()
    return (create_bot(token), True) if token else (None, False)


async def retire_ticket_notices(session: AsyncSession, ticket: Ticket) -> int:
    """Remove the closed ticket's last notice from each staff chat; return how many were found.

    Best effort: the ticket is already closed, so a failure only leaves the notice on screen.
    """
    bot, should_close = await _notice_bot(session, ticket.reseller_id)
    if bot is None:
        return 0
    try:
        return await clear_notices(session, bot, ticket_notice_key(ticket.id), resolved_text=TICKET_CLOSED_TEXT)
    finally:
        if should_close:
            try:
                await bot.session.close()
            except (TelegramAPIError, OSError):
                logger.debug("ticket notice bot close failed ticket=%s", ticket.id, exc_info=True)
