"""Receipt photos stored as Telegram file ids: which bot owns them and how to download one.

A receipt's ``file_id`` is only valid for the bot that received it, so a shop's receipt
is read with that shop's token and a platform receipt with the platform token, with no
fallback either way.
"""

from __future__ import annotations

import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Order, Payment, ResellerProfile
from app.services.secret_box import reveal_bot_token

RECEIPT_MAX_BYTES = 15 * 1024 * 1024
TELEGRAM_FILE_TIMEOUT_SECONDS = 20.0
_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_IMAGE_TYPES_BY_SUFFIX = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


async def _shop_bot_token(session: AsyncSession, shop_id: int) -> str:
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    return ((reveal_bot_token(profile.bot_token) if profile else None) or "").strip()


async def payment_bot_token(
    session: AsyncSession,
    payment: Payment,
    *,
    order: Order | None,
    payer_reseller_id: int | None,
) -> str:
    """Token of the bot that owns this receipt's file id; empty when that bot has none."""
    if order is not None and order.reseller_id:
        return await _shop_bot_token(session, int(order.reseller_id))
    if payer_reseller_id:
        return await _shop_bot_token(session, int(payer_reseller_id))
    return (get_settings().bot_token or "").strip()


def safe_telegram_file_path(raw: str | None) -> str | None:
    """Normalize Telegram getFile path; reject traversal / absolute / non-file paths."""
    path = (raw or "").strip().lstrip("/")
    if not path or ".." in path or "\\" in path:
        return None
    if path.startswith(("http:", "https:", "file:")) or any(ord(ch) < 32 for ch in path):
        return None
    return path if _FILE_PATH_RE.match(path) else None


def _image_type(content_type: str, path: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype or ctype == "application/octet-stream":
        lower = path.lower()
        ctype = next((t for suffix, t in _IMAGE_TYPES_BY_SUFFIX.items() if lower.endswith(suffix)), "image/jpeg")
    return ctype


async def fetch_telegram_file(
    token: str, file_id: str, *, client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str] | None:
    """Download a receipt photo; None unless it is an image of at most ``RECEIPT_MAX_BYTES``."""
    if client is None:
        async with httpx.AsyncClient(timeout=TELEGRAM_FILE_TIMEOUT_SECONDS) as own_client:
            return await fetch_telegram_file(token, file_id, client=own_client)
    meta = await client.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id})
    data = meta.json() if meta.status_code == 200 else {}
    if not data.get("ok"):
        return None
    path = safe_telegram_file_path((data.get("result") or {}).get("file_path") or "")
    if not path:
        return None
    file_resp = await client.get(f"https://api.telegram.org/file/bot{token}/{path}")
    if file_resp.status_code != 200 or not file_resp.content or len(file_resp.content) > RECEIPT_MAX_BYTES:
        return None
    ctype = _image_type(file_resp.headers.get("content-type") or "", path)
    if not ctype.startswith("image/"):
        return None
    return file_resp.content, ctype
