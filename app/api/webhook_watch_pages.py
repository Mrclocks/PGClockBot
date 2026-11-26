"""«بررسی همین حالا» for the main bot's update delivery (settings → ربات), owner only."""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import Bot
from fastapi import Depends
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.services.webhook_watch import check_webhook

logger = logging.getLogger(__name__)

CHECK_PATH = "/settings/bot/webhook-check"
BOT_TAB = "/settings?tab=bot"


def register_webhook_watch_pages(app: object, *, require_admin: Callable[..., object]) -> None:
    @app.post(CHECK_PATH)
    async def webhook_check_now(staff: dict = Depends(require_admin)) -> RedirectResponse:
        token = (get_settings().bot_token or "").strip()
        if token:
            bot = Bot(token=token)
            try:
                await check_webhook(bot)
            finally:
                await bot.session.close()
        # The bot tab shows the fresh result itself; nothing from the request is echoed back.
        return RedirectResponse(BOT_TAB, status_code=303)
