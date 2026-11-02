"""Every few hours, copy each bot's Telegram profile photo for its Mini App logo."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientError
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionLocal
from app.services.bot_profile_photo import PhotoTarget, platform_target, shop_targets, sync_bot_photo

logger = logging.getLogger(__name__)

SHOP_BATCH = 50
# A few bots at a time: each sync is three or four small Telegram calls.
CONCURRENCY = 3


async def _sync_one(target: PhotoTarget, gate: asyncio.Semaphore) -> None:
    async with gate:
        try:
            outcome = await sync_bot_photo(SessionLocal, target)
        except (TelegramAPIError, ClientError, SQLAlchemyError, OSError, ValueError, asyncio.TimeoutError):
            logger.warning("bot photo sync failed shop=%s", target.shop_id, exc_info=True)
            return
    if outcome in {"saved", "cleared"}:
        logger.info("bot photo %s shop=%s", outcome, target.shop_id)


async def run_bot_profile_photo_sync() -> None:
    """The platform bot, then shops in pages; one failing bot never stops the rest."""
    gate = asyncio.Semaphore(CONCURRENCY)
    platform = platform_target()
    if platform is not None:
        await _sync_one(platform, gate)
    after_id = 0
    while True:
        try:
            async with SessionLocal() as session:
                batch = await shop_targets(session, after_id=after_id, limit=SHOP_BATCH)
        except SQLAlchemyError:
            logger.warning("bot photo sync could not list shops", exc_info=True)
            return
        if not batch:
            return
        await asyncio.gather(*(_sync_one(target, gate) for target in batch))
        after_id = max(int(t.profile_id or 0) for t in batch)
        if len(batch) < SHOP_BATCH:
            return
