"""Scheduler job: check every few minutes that Telegram delivers to this server, repair it, tell the admins."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import get_settings
from app.services.webhook_watch import alert_text, check_webhook, should_alert

logger = logging.getLogger(__name__)


async def run_webhook_watch(bot: Bot) -> None:
    health = await check_webhook(bot)
    if health is None or not should_alert(health):
        return
    text = alert_text(health)
    for admin_id in get_settings().admin_ids:
        try:
            await bot.send_message(int(admin_id), text)
        except TelegramAPIError:
            # One admin who blocked the bot must not stop the others from hearing about it.
            logger.warning("webhook alert to admin %s failed", admin_id, exc_info=True)
