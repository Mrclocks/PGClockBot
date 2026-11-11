"""Atomic one-shot claims stored as unique ``Setting`` keys.

Insert-or-fail under a savepoint so a concurrent duplicate returns False without
rolling back the caller's outer unit of work.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

logger = logging.getLogger(__name__)

KEY_MAX = 128


def claim_setting_key(namespace: str, *parts: object) -> str:
    """Stable Setting.key for a claim; hashed so parts never overflow the column."""
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    key = f"{namespace}:{digest}"
    if len(key) > KEY_MAX:
        raise ValueError("کلید تکرار نامعتبر است")
    return key


async def claim_once(session: AsyncSession, key: str, *, marker: str = "1") -> bool:
    """Return True if this session won the claim; False if the key already exists."""
    if not key or len(key) > KEY_MAX:
        raise ValueError("کلید تکرار نامعتبر است")
    try:
        async with session.begin_nested():
            session.add(Setting(key=key, value=marker))
            await session.flush()
        return True
    except IntegrityError:
        logger.debug("idempotency claim already taken key_prefix=%s", key[:24])
        return False
