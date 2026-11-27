"""A photo or video sent with a group message, from the bot or uploaded in the web panel.

A broadcast carries at most one media item. Media an admin sends to the bot is kept only as its Telegram
file id, which belongs to that bot; a broadcast row is always sent by the same bot that received it (the
platform bot for platform rows, the shop's bot for shop rows). A web upload is stored under
``BROADCAST_MEDIA_DIR`` until the worker's first successful upload returns a file id; the row then keeps the
file id and the local file is removed. Files no active broadcast needs are pruned after a grace period.

The text of a message with media is its caption, which Telegram limits to ``CAPTION_MAX`` characters.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import DATA_DIR
from app.services.guide_media import (
    FILE_ID_MAX,
    KIND_PHOTO,
    KIND_VIDEO,
    MAX_PHOTO_BYTES,
    MAX_VIDEO_BYTES,
    check_upload,
)

BROADCAST_MEDIA_DIR = DATA_DIR / "broadcast_media"
CAPTION_MAX = 1024
MEDIA_KINDS = (KIND_PHOTO, KIND_VIDEO)
MAX_UPLOAD_BYTES = max(MAX_PHOTO_BYTES, MAX_VIDEO_BYTES)
UNUSED_FILE_GRACE_SEC = 10 * 60
MEDIA_LABELS = {KIND_PHOTO: "🖼 عکس", KIND_VIDEO: "🎬 ویدیو"}
CAPTION_TOO_LONG = f"متن همراه عکس یا ویدیو حداکثر {CAPTION_MAX} نویسه است"

_STORED_NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp|mp4)$")


@dataclass(frozen=True, slots=True)
class BroadcastMedia:
    """One photo or video: a Telegram file id of the sending bot, or a stored web upload to send once."""

    kind: str
    file_id: str | None = None
    file_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in MEDIA_KINDS:
            raise ValueError("نوع رسانه نامعتبر است")
        if not (self.file_id or self.file_name):
            raise ValueError("رسانه‌ای انتخاب نشده است")
        if self.file_id is not None and (not self.file_id.strip() or len(self.file_id) > FILE_ID_MAX):
            raise ValueError("شناسهٔ فایل تلگرام نامعتبر است")
        if self.file_name is not None and not _STORED_NAME.match(self.file_name):
            raise ValueError("فایل رسانه نامعتبر است")


def media_from_row(kind: str | None, file_id: str | None, file_name: str | None) -> BroadcastMedia | None:
    """The media a stored broadcast row carries; ``None`` for a text-only row or unreadable values."""
    if not kind:
        return None
    try:
        return BroadcastMedia(kind=kind, file_id=file_id or None, file_name=file_name or None)
    except ValueError:
        return None


def media_label(kind: str | None) -> str:
    return MEDIA_LABELS.get(kind or "", "")


def text_limit(*, with_media: bool, default: int) -> int:
    """How long the message text may be: the caption limit with media, else ``default``."""
    return CAPTION_MAX if with_media else default


def scope_dir(shop_id: int | None) -> Path:
    """Platform and each shop keep their uploads apart."""
    return BROADCAST_MEDIA_DIR / ("platform" if shop_id is None else f"shop-{int(shop_id)}")


def check_media_upload(data: bytes) -> tuple[str, str]:
    """(kind, extension) of an uploaded photo or video; raises ``ValueError`` with a safe reason."""
    return check_upload(data)


def write_upload(data: bytes, *, shop_id: int | None) -> BroadcastMedia:
    """Blocking: validate and store an upload; call through ``asyncio.to_thread``."""
    kind, extension = check_media_upload(data)
    folder = scope_dir(shop_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}.{extension}"
    (folder / name).write_bytes(data)
    return BroadcastMedia(kind=kind, file_name=name)


def stored_path(file_name: str | None, *, shop_id: int | None) -> Path | None:
    """Path of a stored upload, only for names this module generated; ``None`` when missing."""
    if not file_name or not _STORED_NAME.match(file_name):
        return None
    path = scope_dir(shop_id) / file_name
    return path if path.is_file() else None


def delete_upload(file_name: str | None, *, shop_id: int | None) -> None:
    """Blocking delete; call through ``asyncio.to_thread``."""
    path = stored_path(file_name, shop_id=shop_id)
    if path is not None:
        path.unlink(missing_ok=True)


def prune_uploads(in_use: set[tuple[int | None, str]], *, now: float | None = None) -> int:
    """Blocking: remove uploads no active broadcast needs, once they are older than the grace period.

    ``in_use`` holds ``(shop_id, file_name)`` of active rows; the grace period covers an upload whose
    broadcast row is still being written.
    """
    if not BROADCAST_MEDIA_DIR.is_dir():
        return 0
    current = time.time() if now is None else now
    removed = 0
    for folder in BROADCAST_MEDIA_DIR.iterdir():
        shop_id = _folder_shop_id(folder.name)
        if shop_id is False or not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not _STORED_NAME.match(path.name) or (shop_id, path.name) in in_use or not path.is_file():
                continue
            if current - path.stat().st_mtime < UNUSED_FILE_GRACE_SEC:
                continue
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _folder_shop_id(name: str) -> int | None | bool:
    """The tenant of an upload folder: ``None`` for the platform, a shop id, or ``False`` for a stray folder."""
    if name == "platform":
        return None
    match = re.fullmatch(r"shop-([1-9][0-9]{0,18})", name)
    return int(match.group(1)) if match else False
