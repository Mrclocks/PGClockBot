"""Scheduler job: send queued customer notices in a throttled batch."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from app.db.session import SessionLocal
from app.services.customer_action_notices import deliver_notice
from app.services.customer_notice_queue import process_notice_batch
from app.services.reseller_bots import open_notify_bot_for_reseller

logger = logging.getLogger(__name__)

SEND_PAUSE_SECONDS = 0.05  # about 20 messages per second, below Telegram's overall limit


async def run_customer_notices(bot: Bot) -> None:
    """Send one batch; the platform bot serves platform customers, each shop's own bot its customers."""
    opened: dict[int, tuple[Bot | None, bool]] = {}
    try:
        async with SessionLocal() as session:

            async def bot_for(shop_id: int | None) -> Bot | None:
                if shop_id is None:
                    return bot
                if int(shop_id) not in opened:
                    opened[int(shop_id)] = await open_notify_bot_for_reseller(session, int(shop_id))
                return opened[int(shop_id)][0]

            async def send(notice):
                outcome = await deliver_notice(session, bot_for, notice)
                await asyncio.sleep(SEND_PAUSE_SECONDS)
                return outcome

            await process_notice_batch(session, send)
    except Exception:  # noqa: BLE001 - a failed batch is logged and retried on the next tick
        logger.exception("customer notice batch failed")
    finally:
        for shop_bot, should_close in opened.values():
            if shop_bot is not None and should_close:
                await shop_bot.session.close()
