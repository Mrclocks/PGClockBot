"""Photos, videos and video links attached to connection guides.

A guide holds at most ``MAX_MEDIA_PER_GUIDE`` media items and ``MAX_LINKS_PER_GUIDE`` links.
A media item is either a file uploaded from the web panel (kept in ``GUIDE_MEDIA_DIR``, which
backups leave out on purpose) or a file an admin sent to the bot (only its Telegram file id).
Telegram file ids belong to one bot, so the id each bot gets after its first upload of a web
file is cached per bot in ``FILE_ID_CACHE_KEY``; losing that cache only costs a re-upload.

Media entries are owned by the server: a form save keeps the stored entries of each guide and
never takes file names or file ids from the browser.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.services.users import get_setting, set_setting

GUIDE_MEDIA_DIR = DATA_DIR / "guide_media"
FILE_ID_CACHE_KEY = "connection_guides_file_ids"
MAX_MEDIA_PER_GUIDE = 10  # one Telegram album
MAX_LINKS_PER_GUIDE = 3
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # Telegram's photo limit
MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Telegram's upload limit for bots
LINK_LABEL_MAX = 48
LINK_URL_MAX = 512
FILE_ID_MAX = 256
UNUSED_FILE_GRACE_SEC = 10 * 60
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
ALLOWED_LINK_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "t.me", "telegram.me"}
)
DEFAULT_LINK_LABEL = "▶️ دیدن ویدیو"

_MEDIA_ID = re.compile(r"^[0-9a-f]{12}$")
_STORED_NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp|mp4)$")
_BOT_ID = re.compile(r"^[0-9]{1,20}$")


class GuideMediaError(ValueError):
    """A media or link change the admin must correct; the message is safe to show."""


def detect_media(head: bytes) -> tuple[str, str] | None:
    """(kind, extension) from the file's magic bytes; ``None`` for anything else."""
    if head.startswith(b"\xff\xd8\xff"):
        return KIND_PHOTO, "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return KIND_PHOTO, "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return KIND_PHOTO, "webp"
    if head[4:8] == b"ftyp":
        return KIND_VIDEO, "mp4"
    return None


def check_upload(data: bytes) -> tuple[str, str]:
    detected = detect_media(data[:16])
    if detected is None:
        raise GuideMediaError("فقط عکس (JPG، PNG، WebP) یا ویدیوی MP4 پذیرفته می‌شود.")
    kind, _ext = detected
    limit = MAX_PHOTO_BYTES if kind == KIND_PHOTO else MAX_VIDEO_BYTES
    if not data or len(data) > limit:
        raise GuideMediaError(f"حجم فایل باید کمتر از {limit // (1024 * 1024)} مگابایت باشد.")
    return detected


def scope_dir(reseller_id: int | None) -> Path:
    """Platform and each shop keep their files apart."""
    return GUIDE_MEDIA_DIR / ("platform" if reseller_id is None else f"shop-{int(reseller_id)}")


def write_media_file(data: bytes, extension: str, *, reseller_id: int | None) -> str:
    """Blocking write; call through ``asyncio.to_thread``. Returns the stored file name."""
    folder = scope_dir(reseller_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{extension}"
    (folder / name).write_bytes(data)
    return name


def media_path(name: str | None, *, reseller_id: int | None) -> Path | None:
    """Path of a stored file, only for names this module generated; ``None`` when missing."""
    if not name or not _STORED_NAME.match(name):
        return None
    path = scope_dir(reseller_id) / name
    return path if path.is_file() else None


def delete_media_file(name: str | None, *, reseller_id: int | None) -> None:
    """Blocking delete; call through ``asyncio.to_thread``."""
    path = media_path(name, reseller_id=reseller_id)
    if path is not None:
        path.unlink(missing_ok=True)


def delete_unused_files(used: set[str], *, reseller_id: int | None) -> int:
    """Blocking; remove this scope's files no guide uses (a guide deleted from the form)."""
    folder = scope_dir(reseller_id)
    if not folder.is_dir():
        return 0
    removed = 0
    for path in folder.iterdir():
        if not _STORED_NAME.match(path.name) or path.name in used or not path.is_file():
            continue
        if time.time() - path.stat().st_mtime < UNUSED_FILE_GRACE_SEC:
            continue  # an upload whose guide entry is still being saved
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def new_media_id() -> str:
    return secrets.token_hex(6)


def _clean_file_ids(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(bot_id): str(file_id)[:FILE_ID_MAX]
        for bot_id, file_id in raw.items()
        if _BOT_ID.match(str(bot_id)) and isinstance(file_id, str) and file_id.strip()
    }


def parse_media(raw: Any) -> list[dict[str, Any]]:
    """Stored media entries, dropping anything malformed."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or len(out) >= MAX_MEDIA_PER_GUIDE:
            continue
        media_id = str(item.get("id") or "")
        kind = item.get("kind")
        file_name = item.get("file")
        file_name = file_name if isinstance(file_name, str) and _STORED_NAME.match(file_name) else None
        file_ids = _clean_file_ids(item.get("tg"))
        if not _MEDIA_ID.match(media_id) or kind not in (KIND_PHOTO, KIND_VIDEO) or not (file_name or file_ids):
            continue
        entry: dict[str, Any] = {"id": media_id, "kind": kind}
        if file_name:
            entry["file"] = file_name
        if file_ids:
            entry["tg"] = file_ids
        out.append(entry)
    return out


def normalize_link(label: Any, url: Any) -> dict[str, str]:
    """One validated link; raises ``GuideMediaError`` with the reason."""
    clean_url = str(url or "").strip()
    if len(clean_url) > LINK_URL_MAX:
        raise GuideMediaError("لینک خیلی بلند است.")
    parts = urlsplit(clean_url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_LINK_HOSTS:
        raise GuideMediaError("فقط لینک https از یوتیوب (youtube.com، youtu.be) یا تلگرام (t.me) پذیرفته می‌شود.")
    if parts.username or parts.password:
        raise GuideMediaError("لینک نامعتبر است.")
    clean_label = re.sub(r"[<>\"'`]", "", str(label or "")).strip()[:LINK_LABEL_MAX] or DEFAULT_LINK_LABEL
    return {"label": clean_label, "url": clean_url}


def parse_links(raw: Any) -> list[dict[str, str]]:
    """Stored or submitted links, dropping invalid ones."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or len(out) >= MAX_LINKS_PER_GUIDE:
            continue
        try:
            out.append(normalize_link(item.get("label"), item.get("url")))
        except GuideMediaError:
            continue
    return out


def keep_stored_media(submitted: list[dict[str, Any]], stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each submitted guide the media stored for the same guide id, whatever the form sent."""
    stored_media = {str(g.get("id")): g.get("media") or [] for g in stored}
    out = []
    for guide in submitted:
        entry = {key: value for key, value in guide.items() if key != "media"}
        media = stored_media.get(str(guide.get("id")))
        if media:
            entry["media"] = media
        out.append(entry)
    return out


def stored_files(guides: list[dict[str, Any]]) -> set[str]:
    return {m["file"] for g in guides for m in g.get("media") or [] if m.get("file")}


def media_file_id(entry: dict[str, Any], bot_id: int, cache: dict[str, dict[str, str]]) -> str | None:
    key = str(bot_id)
    return (entry.get("tg") or {}).get(key) or (cache.get(entry["id"]) or {}).get(key)


def is_media_missing(entry: dict[str, Any], *, reseller_id: int | None) -> bool:
    """A web upload whose file is gone (for example after a restore) and no bot has a copy."""
    return not entry.get("tg") and media_path(entry.get("file"), reseller_id=reseller_id) is None


@dataclass(frozen=True, slots=True)
class MediaToSend:
    media_id: str
    kind: str
    file_id: str | None  # a Telegram copy this bot already has
    path: Path | None  # otherwise the stored file to upload


def media_to_send(
    guide: dict[str, Any], *, bot_id: int, cache: dict[str, dict[str, str]], reseller_id: int | None
) -> list[MediaToSend]:
    """The guide's album for one bot, skipping items this bot can neither reuse nor upload."""
    out = []
    for entry in guide.get("media") or []:
        file_id = media_file_id(entry, bot_id, cache)
        path = None if file_id else media_path(entry.get("file"), reseller_id=reseller_id)
        if file_id or path:
            out.append(MediaToSend(media_id=entry["id"], kind=entry["kind"], file_id=file_id, path=path))
    return out


async def load_file_id_cache(session: AsyncSession, *, reseller_id: int | None) -> dict[str, dict[str, str]]:
    raw = await get_setting(session, FILE_ID_CACHE_KEY, "", reseller_id=reseller_id)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): _clean_file_ids(v) for k, v in data.items() if _MEDIA_ID.match(str(k))}


async def remember_file_ids(
    session: AsyncSession,
    learned: dict[str, str],
    *,
    bot_id: int,
    live_media_ids: set[str],
    reseller_id: int | None,
) -> None:
    """Cache the file ids a bot got for web uploads; ids of deleted media are dropped."""
    if not learned:
        return
    cache = await load_file_id_cache(session, reseller_id=reseller_id)
    cache = {media_id: ids for media_id, ids in cache.items() if media_id in live_media_ids}
    for media_id, file_id in learned.items():
        cache.setdefault(media_id, {})[str(bot_id)] = file_id[:FILE_ID_MAX]
    await set_setting(session, FILE_ID_CACHE_KEY, json.dumps(cache), reseller_id=reseller_id)
