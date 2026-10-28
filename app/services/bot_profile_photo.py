"""Keep a local copy of a bot's Telegram profile photo for the Mini App logo.

The Mini App shows ``bot_tg_photo`` as the shop logo. A photo uploaded on the panel's
«هویت ربات» tab is stored there already; a photo set directly in @BotFather is not. This
module copies the latter from Telegram and refreshes the copy when the photo changes.

A panel upload always wins: the copy is written only while there is no photo or the
current one came from Telegram (``bot_tg_photo_source`` = ``telegram:<file_unique_id>``).
Network calls never run inside a database transaction: callers read the settings, close
the session, fetch, then store in a new session.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import DATA_DIR, get_settings
from app.db.models import ResellerProfile, ResellerSetting
from app.services.bot_appearance import PHOTO_SOURCE_SETTING as SOURCE_SETTING
from app.services.miniapp_shop import PERMISSION, PLATFORM_SWITCH, SHOP_SWITCH
from app.services.resellers import has_perm
from app.services.secret_box import reveal_bot_token
from app.services.users import get_all_settings, get_setting, on, set_settings_bulk

PHOTO_SETTING = "bot_tg_photo"
TELEGRAM_SOURCE_PREFIX = "telegram:"
# The logo is drawn at 50px (100px on retina screens); the smallest size at least this wide is enough.
LOGO_MIN_SIDE = 160
PHOTO_MAX_BYTES = 2 * 1024 * 1024
TELEGRAM_TIMEOUT_SECONDS = 20
_SAFE_UNIQUE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STORED_TELEGRAM_PHOTO = re.compile(r"^uploads/[A-Za-z0-9_-]+_bot_tg_photo_tg_[0-9a-f]{12}\.jpg$")

SyncOutcome = Literal["skipped", "unchanged", "saved", "cleared"]


@dataclass(frozen=True, slots=True)
class ProfilePhoto:
    unique_id: str
    data: bytes


def uploads_dir() -> Path:
    return DATA_DIR / "uploads"


def pick_photo_size(sizes: Sequence, *, min_side: int = LOGO_MIN_SIDE) -> object | None:
    """The smallest size at least ``min_side`` wide, else the largest there is."""
    if not sizes:
        return None
    ordered = sorted(sizes, key=lambda s: (int(getattr(s, "width", 0) or 0), int(getattr(s, "height", 0) or 0)))
    for size in ordered:
        if min(int(size.width or 0), int(size.height or 0)) >= min_side:
            return size
    return ordered[-1]


async def fetch_bot_profile_photo(token: str) -> ProfilePhoto | None:
    """The bot's current profile photo, or None when it has none. Raises on Telegram errors."""
    bot = Bot(token=token)
    try:
        me = await bot.get_me(request_timeout=TELEGRAM_TIMEOUT_SECONDS)
        photos = await bot.get_user_profile_photos(me.id, limit=1, request_timeout=TELEGRAM_TIMEOUT_SECONDS)
        size = pick_photo_size(photos.photos[0]) if photos.photos else None
        if size is None:
            return None
        if not _SAFE_UNIQUE_ID.fullmatch(str(size.file_unique_id or "")):
            raise ValueError("unexpected file_unique_id from Telegram")
        if int(size.file_size or 0) > PHOTO_MAX_BYTES:
            raise ValueError("profile photo is larger than the logo limit")
        file = await bot.get_file(size.file_id, request_timeout=TELEGRAM_TIMEOUT_SECONDS)
        buffer = io.BytesIO()
        await bot.download_file(file.file_path, destination=buffer, timeout=TELEGRAM_TIMEOUT_SECONDS)
        data = buffer.getvalue()
        if not data or len(data) > PHOTO_MAX_BYTES:
            raise ValueError("profile photo download is empty or too large")
        return ProfilePhoto(unique_id=str(size.file_unique_id), data=data)
    finally:
        await bot.session.close()


def wants_telegram_photo(ui: Mapping[str, str]) -> bool:
    """True when there is no photo yet or the current one is a copy from Telegram."""
    photo = (ui.get(PHOTO_SETTING) or "").strip()
    source = (ui.get(SOURCE_SETTING) or "").strip()
    return not photo or source.startswith(TELEGRAM_SOURCE_PREFIX)


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _remove_old_copy(stored: str) -> None:
    """Delete a previous Telegram copy; panel uploads and anything unexpected are left alone."""
    if not _STORED_TELEGRAM_PHOTO.fullmatch(stored):
        return
    try:
        (uploads_dir() / Path(stored).name).unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning("could not delete old bot photo copy %s", stored, exc_info=True)


async def store_telegram_photo(
    session: AsyncSession, *, reseller_id: int | None, ui: Mapping[str, str], photo: ProfilePhoto | None,
) -> SyncOutcome:
    """Save, keep or clear the Telegram copy for one bot (``reseller_id`` None = platform)."""
    if not wants_telegram_photo(ui):
        return "skipped"
    stored = (ui.get(PHOTO_SETTING) or "").strip()
    source = (ui.get(SOURCE_SETTING) or "").strip()
    if photo is None:
        if not stored:
            return "unchanged"
        await set_settings_bulk(session, {PHOTO_SETTING: "", SOURCE_SETTING: ""}, reseller_id=reseller_id)
        await asyncio.to_thread(_remove_old_copy, stored)
        return "cleared"
    new_source = TELEGRAM_SOURCE_PREFIX + photo.unique_id
    if stored and source == new_source:
        return "unchanged"
    prefix = f"r{int(reseller_id)}" if reseller_id else "main"
    name = f"{prefix}_bot_tg_photo_tg_{uuid.uuid4().hex[:12]}.jpg"
    await asyncio.to_thread(_write_file, uploads_dir() / name, photo.data)
    await set_settings_bulk(
        session, {PHOTO_SETTING: f"uploads/{name}", SOURCE_SETTING: new_source}, reseller_id=reseller_id,
    )
    if stored:
        await asyncio.to_thread(_remove_old_copy, stored)
    return "saved"


@dataclass(frozen=True, slots=True)
class PhotoTarget:
    """One bot whose Mini App shows a logo; ``shop_id`` None is the platform bot."""

    shop_id: int | None
    profile_id: int | None
    # Opens the bot; never logged or returned.
    token: str = field(default="", repr=False)


def platform_target() -> PhotoTarget | None:
    settings = get_settings()
    token = (settings.bot_token or "").strip()
    return PhotoTarget(shop_id=None, profile_id=None, token=token) if settings.miniapp_enabled and token else None


async def shop_targets(session: AsyncSession, *, after_id: int, limit: int) -> list[PhotoTarget]:
    """Shops with their Mini App on, in profile-id order after ``after_id`` (keyset paging)."""
    if not on(await get_setting(session, PLATFORM_SWITCH, "0")):
        return []
    switched_on = select(ResellerSetting.reseller_user_id).where(
        ResellerSetting.key == SHOP_SWITCH, ResellerSetting.value == "1",
    )
    profiles = await session.scalars(
        select(ResellerProfile)
        .where(
            ResellerProfile.id > after_id,
            ResellerProfile.is_active.is_(True),
            ResellerProfile.setup_completed_at.is_not(None),
            ResellerProfile.bot_token.is_not(None),
            ResellerProfile.user_id.in_(switched_on),
        )
        .order_by(ResellerProfile.id)
        .limit(limit)
    )
    targets = []
    for profile in profiles:
        token = reveal_bot_token(profile.bot_token) if has_perm(profile, PERMISSION) else None
        # Keep the id even without a token so paging moves past this shop.
        targets.append(PhotoTarget(shop_id=int(profile.user_id), profile_id=int(profile.id), token=token or ""))
    return targets


async def sync_bot_photo(sessions: async_sessionmaker[AsyncSession], target: PhotoTarget) -> SyncOutcome:
    """Read settings, fetch from Telegram with no session open, then store in a fresh session."""
    if not target.token:
        return "skipped"
    async with sessions() as session:
        ui = await get_all_settings(session, reseller_id=target.shop_id)
    if not wants_telegram_photo(ui):
        return "skipped"
    photo = await fetch_bot_profile_photo(target.token)
    async with sessions() as session:
        # Read again: a panel upload during the fetch must win.
        ui = await get_all_settings(session, reseller_id=target.shop_id)
        return await store_telegram_photo(session, reseller_id=target.shop_id, ui=ui, photo=photo)
