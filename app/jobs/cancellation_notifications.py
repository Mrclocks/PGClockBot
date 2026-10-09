"""Deliver cancellation notices through the bot and staff of the owning shop."""
from __future__ import annotations

import asyncio
import logging
from functools import partial

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.cancellation_notifications import CancellationNotice, NOTIFY_KEY, NoticeOutcome, process_cancellation_notifications
from app.services.notifications import _shop_recipient_chat_ids, notify_enabled
from app.services.reseller_bots import open_notify_bot_for_reseller

logger = logging.getLogger(__name__)


async def _send_notice(bot: Bot, notice: CancellationNotice) -> NoticeOutcome:
    send_bot, should_close = bot, False
    async with SessionLocal() as session:
        if not await notify_enabled(session, NOTIFY_KEY, reseller_id=notice.shop_id):
            return "skipped"
        if notice.shop_id is None:
            targets = list(dict.fromkeys(get_settings().admin_ids))
        else:
            targets = await _shop_recipient_chat_ids(session, notice.shop_id, NOTIFY_KEY)
            send_bot, should_close = await open_notify_bot_for_reseller(session, notice.shop_id)
        await session.commit()
    if send_bot is None:
        return "pending"
    try:
        if not targets:
            return "pending"
        return await _send_to_targets(send_bot, notice, targets)
    finally:
        if should_close:
            await send_bot.session.close()


async def _send_to_targets(bot: Bot, notice: CancellationNotice, targets: list[int]) -> NoticeOutcome:
    base_url = get_settings().public_base_url.rstrip("/")
    markup = None
    if base_url.startswith("https://"):
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بررسی درخواست‌های لغو", url=base_url + "/finance?tab=cancellations"),
        ]])
    outcome: NoticeOutcome = "sent"
    for chat_id in targets:
        if not await _send_target(bot, notice, chat_id, markup):
            outcome = "review"
        await asyncio.sleep(1)
    return outcome


async def _send_target(
    bot: Bot, notice: CancellationNotice, chat_id: int, markup: InlineKeyboardMarkup | None,
) -> bool:
    for attempt in range(2):
        try:
            await asyncio.wait_for(bot.send_message(chat_id, notice.text, parse_mode="HTML", reply_markup=markup), timeout=20)
            return True
        except TelegramRetryAfter as exc:
            if attempt == 0 and exc.retry_after <= 60:
                await asyncio.sleep(exc.retry_after)
                continue
            logger.warning("cancellation notice rate limited request=%s chat=%s", notice.request_id, chat_id, exc_info=True)
            return False
        except (TelegramAPIError, OSError, TimeoutError):
            # The other recipients may already have received this notice.
            logger.warning("cancellation notice delivery needs review request=%s chat=%s", notice.request_id, chat_id, exc_info=True)
            return False
    return False


async def run_cancellation_notifications(bot: Bot) -> None:
    try:
        async with SessionLocal() as session:
            await process_cancellation_notifications(session, partial(_send_notice, bot))
    except Exception:
        logger.exception("cancellation notification batch failed")
