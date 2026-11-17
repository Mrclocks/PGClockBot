"""Backup retention and Telegram delivery settings: allowed values and normalisation."""

from __future__ import annotations

import asyncio
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.backup import prune_backups
from app.services.secret_box import seal_bot_token
from app.services.users import get_setting

KEEP_CHOICES = (5, 10, 15)
DEFAULT_KEEP = 10
KEEP_KEY = "backup_keep_count"
TOKEN_KEY = "backup_telegram_bot_token"
CHAT_IDS_KEY = "backup_telegram_chat_ids"
MAX_RECIPIENTS = 10

_TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,60}$")
_CHAT_ID_RE = re.compile(r"^-?\d{3,20}$")


def keep_count(raw: object) -> int:
    """The configured number of backups to keep; anything unexpected falls back to the default."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_KEEP
    return value if value in KEEP_CHOICES else DEFAULT_KEEP


def parse_chat_ids(raw: str | None) -> list[int]:
    """Telegram chat ids from comma/space/newline separated text; invalid parts are ignored."""
    parts = re.split(r"[,\s]+", (raw or "").strip())
    seen: list[int] = []
    for part in parts:
        if _CHAT_ID_RE.match(part) and int(part) not in seen:
            seen.append(int(part))
    return seen[:MAX_RECIPIENTS]


def normalize_backup_setting(key: str, value: str) -> str:
    """Validate one submitted backup setting; raises ``ValueError`` with a message for the admin."""
    cleaned = (value or "").strip()
    if key == KEEP_KEY:
        if cleaned not in {str(choice) for choice in KEEP_CHOICES}:
            raise ValueError("تعداد بکاپ نگه‌داشته‌شده باید ۵، ۱۰ یا ۱۵ باشد.")
        return cleaned
    if key == TOKEN_KEY:
        if not _TOKEN_RE.match(cleaned):
            raise ValueError("توکن ربات بکاپ معتبر نیست؛ آن را از BotFather کپی کنید.")
        return seal_bot_token(cleaned) or ""
    if key == CHAT_IDS_KEY:
        ids = parse_chat_ids(cleaned)
        if cleaned and not ids:
            raise ValueError("شناسهٔ چت گیرنده باید عدد باشد (چند شناسه را با کاما جدا کنید).")
        return ", ".join(str(chat_id) for chat_id in ids)
    return cleaned


def normalize_backup_payload(payload: dict[str, str]) -> str | None:
    """Validate and normalise the backup settings of a submitted tab in place.

    Returns a message for the admin when a value is refused, otherwise ``None``.
    """
    for key in (KEEP_KEY, TOKEN_KEY, CHAT_IDS_KEY):
        if key in payload:
            try:
                payload[key] = normalize_backup_setting(key, payload[key])
            except ValueError as exc:
                return str(exc)
    return None


async def apply_backup_retention(payload: dict[str, str]) -> int:
    """Delete the backups beyond the retention just saved; returns how many were removed."""
    if KEEP_KEY not in payload:
        return 0
    return await asyncio.to_thread(prune_backups, keep_count(payload[KEEP_KEY]))


async def prune_to_configured_retention(session: AsyncSession) -> int:
    """Apply the saved retention after a new backup; returns how many old backups were removed."""
    keep = keep_count(await get_setting(session, KEEP_KEY, str(DEFAULT_KEEP)))
    return await asyncio.to_thread(prune_backups, keep)
