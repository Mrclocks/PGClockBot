"""Hourly removal of chat-cleanup rows past Telegram's delete window."""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal
from app.services.chat_message_ledger import purge_expired

logger = logging.getLogger(__name__)


async def run_chat_message_purge() -> None:
    """One bounded batch per run; a backlog shrinks over the following runs."""
    try:
        async with SessionLocal() as session:
            removed = await purge_expired(session)
    except SQLAlchemyError:
        logger.warning("chat message purge failed", exc_info=True)
        return
    if removed:
        logger.info("chat message purge removed=%s", removed)
