"""Scheduler job: tell staff when someone they are waiting for has started the bot."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.db.session import SessionLocal
from app.services.db_safe import rollback_quiet
from app.services.start_watch import arrived_start_watches, claim_start_watch, purge_start_watches

logger = logging.getLogger(__name__)

START_WATCH_CHECK_MINUTES = 2
MAKE_RESELLER_LABEL = "➕ نماینده کن"


def arrival_text(user: BotUser) -> str:
    handle = f" @{html.escape(user.username)}" if user.username else ""
    name = html.escape(user.full_name or "—")
    return f"👤 <b>{name}</b>{handle} ربات را استارت کرد.\nآیدی عددی: <code>{int(user.telegram_id)}</code>"


async def run_start_watch(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        await purge_start_watches(session, now=now)
        arrived = await arrived_start_watches(session, now=now)
        for watch, user in arrived:
            await _announce(bot, session, watch.id, int(watch.watcher_telegram_id), user, now=now)


async def _announce(bot: Bot, session: AsyncSession, watch_id: int, watcher: int, user: BotUser, *, now: datetime) -> None:
    """One watch; a failure is logged and the next watch still runs."""
    try:
        # Claimed (and committed) before the message, so an overlapping run never sends it twice.
        if not await claim_start_watch(session, watch_id, now=now):
            return
    except SQLAlchemyError:
        logger.warning("start watch %s: claim failed", watch_id, exc_info=True)
        await rollback_quiet(session)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=MAKE_RESELLER_LABEL, callback_data=f"stw:mk:{int(user.id)}")]]
    )
    try:
        await bot.send_message(watcher, arrival_text(user), reply_markup=markup)
    except TelegramAPIError:
        logger.warning("start watch %s: notice to watcher failed", watch_id, exc_info=True)
