"""One-time, short-lived panel login links for the shop-owner credentials card.

The token itself is never logged. Consumption is atomic (delete-then-check) so a
second open fails closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

logger = logging.getLogger(__name__)

TOKEN_BYTES = 24
TTL_SECONDS = 300
KEY_PREFIX = "panel_open:"
TOKEN_MAX_LEN = 64


def _setting_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    return f"{KEY_PREFIX}{digest}"


async def mint_panel_open_token(session: AsyncSession, *, shop_user_id: int) -> str:
    """Return a fresh opaque token; store only its hash and expiry."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires = int(datetime.now(timezone.utc).timestamp()) + TTL_SECONDS
    payload = json.dumps({"uid": int(shop_user_id), "exp": expires}, separators=(",", ":"))
    session.add(Setting(key=_setting_key(token), value=payload))
    await session.commit()
    return token


async def consume_panel_open_token(session: AsyncSession, token: str) -> int | None:
    """Consume the token once. Returns shop user id or None when invalid/expired/used."""
    raw = (token or "").strip()
    if not raw or len(raw) > TOKEN_MAX_LEN:
        return None
    key = _setting_key(raw)
    row = await session.scalar(select(Setting).where(Setting.key == key).limit(1))
    if row is None:
        return None
    # Delete first so a concurrent consumer cannot reuse the same row.
    await session.execute(delete(Setting).where(Setting.key == key))
    await session.commit()
    try:
        data = json.loads(row.value or "")
        uid = int(data["uid"])
        exp = int(data["exp"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        logger.warning("panel_open token payload unreadable")
        return None
    now = int(datetime.now(timezone.utc).timestamp())
    if exp < now or uid <= 0:
        return None
    return uid


def panel_open_path(token: str) -> str:
    return f"/r/panel-open/{token}"
