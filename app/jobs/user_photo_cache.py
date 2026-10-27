"""Once a day, drop cached customer profile photos nobody opened for a week."""

from __future__ import annotations

import asyncio
import logging

from app.services.user_photo import prune_photo_cache

logger = logging.getLogger(__name__)


async def run_user_photo_prune() -> None:
    removed = await asyncio.to_thread(prune_photo_cache)
    if removed:
        logger.info("pruned %s cached profile photos", removed)
