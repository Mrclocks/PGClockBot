"""A customer's Telegram profile photo for the panel's user card, cached on disk per bot.

Each scope sees only what its own bot can see: the photo is fetched with the viewer's bot
(a shop's own bot, or the platform bot for the owner) and cached under that scope, so one
shop's copy is never served to another. The bot token stays on the server; the browser
only receives the image bytes from a scoped panel route.

The cache is refreshed after ``CACHE_SECONDS`` and an empty file records "no photo", so
Telegram is asked at most once a day per customer and scope. ``prune_photo_cache`` removes
copies not refreshed for ``PRUNE_AFTER_SECONDS``, that is customers nobody opened lately.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, get_settings
from app.db.models import ResellerProfile
from app.services.bot_profile_photo import pick_photo_size
from app.services.secret_box import reveal_bot_token

logger = logging.getLogger(__name__)

CACHE_SECONDS = 24 * 3600
PRUNE_AFTER_SECONDS = 7 * 24 * 3600
# The card draws the photo at 48px (96px on retina screens).
PHOTO_MIN_SIDE = 96
PHOTO_MAX_BYTES = 256 * 1024
TELEGRAM_TIMEOUT_SECONDS = 8
JPEG_MAGIC = b"\xff\xd8\xff"
PLATFORM_SCOPE = "main"


@dataclass(frozen=True, slots=True)
class PhotoBot:
    """The bot whose view of a customer the card shows; ``scope`` names its cache folder."""

    scope: str
    # Opens the bot; never logged or returned.
    token: str = field(repr=False)


def photo_root() -> Path:
    return DATA_DIR / "user_photos"


def cache_path(scope: str, bot_user_id: int) -> Path:
    return photo_root() / scope / f"{int(bot_user_id)}.jpg"


async def photo_bot(session: AsyncSession, *, shop_id: int | None) -> PhotoBot | None:
    """The viewer's bot: the shop's own bot for shop staff, the platform bot for the owner."""
    if shop_id is None:
        token = (get_settings().bot_token or "").strip()
        return PhotoBot(scope=PLATFORM_SCOPE, token=token) if token else None
    stored = await session.scalar(select(ResellerProfile.bot_token).where(ResellerProfile.user_id == int(shop_id)))
    token = (reveal_bot_token(stored) or "").strip()
    return PhotoBot(scope=f"s{int(shop_id)}", token=token) if token else None


def _read_fresh(path: Path) -> bytes | None:
    """Cached bytes (b"" = known to have no photo), or None when missing or older than a day."""
    try:
        if time.time() - path.stat().st_mtime > CACHE_SECONDS:
            return None
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def fetch_profile_photo(token: str, telegram_id: int) -> bytes:
    """The user's current photo as seen by this bot; b"" when they have none or hide it.

    Raises ``TelegramAPIError``, ``aiohttp.ClientError`` or ``asyncio.TimeoutError`` when Telegram
    could not answer; ``TelegramBadRequest`` (an unknown user) counts as no photo.
    """
    bot = Bot(token=token)
    try:
        try:
            photos = await bot.get_user_profile_photos(
                int(telegram_id), limit=1, request_timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
        except TelegramBadRequest:
            return b""
        size = pick_photo_size(photos.photos[0], min_side=PHOTO_MIN_SIDE) if photos.photos else None
        if size is None or int(size.file_size or 0) > PHOTO_MAX_BYTES:
            return b""
        file = await bot.get_file(size.file_id, request_timeout=TELEGRAM_TIMEOUT_SECONDS)
        buffer = io.BytesIO()
        await bot.download_file(file.file_path, destination=buffer, timeout=TELEGRAM_TIMEOUT_SECONDS)
        data = buffer.getvalue()
        return data if len(data) <= PHOTO_MAX_BYTES and data.startswith(JPEG_MAGIC) else b""
    finally:
        await bot.session.close()


async def user_photo(bot: PhotoBot, *, bot_user_id: int, telegram_id: int) -> bytes | None:
    """JPEG bytes for the card, or None when there is no photo or Telegram could not answer.

    Call with no database transaction open: a cache miss waits on Telegram.
    """
    path = cache_path(bot.scope, bot_user_id)
    cached = await asyncio.to_thread(_read_fresh, path)
    if cached is not None:
        return cached or None
    try:
        data = await fetch_profile_photo(bot.token, telegram_id)
    except (TelegramAPIError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # The exception text can carry the bot's file URL, which holds its token.
        logger.warning("profile photo for user %s not fetched: %s", bot_user_id, type(exc).__name__)
        return None
    await asyncio.to_thread(_write_atomic, path, data)
    return data or None


def prune_photo_cache(*, now: float | None = None) -> int:
    """Delete copies not refreshed for ``PRUNE_AFTER_SECONDS``; returns how many went."""
    cutoff = (time.time() if now is None else now) - PRUNE_AFTER_SECONDS
    removed = 0
    root = photo_root()
    if not root.is_dir():
        return 0
    for path in root.glob("*/*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("could not prune cached profile photo %s", path.name, exc_info=True)
    return removed
