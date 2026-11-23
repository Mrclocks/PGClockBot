"""Warn a reseller once when a capacity meter passes its threshold."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def capacity_used_pct(meter: dict | None) -> float | None:
    if not meter:
        return None
    pct = meter.get("pct")
    try:
        return float(pct) if pct is not None else None
    except Exception:
        return None


def capacity_should_warn(meters: list[dict | None], threshold: float = 80.0) -> bool:
    for m in meters:
        pct = capacity_used_pct(m)
        if pct is not None and pct >= threshold:
            return True
    return False


async def maybe_warn_reseller_capacity(
    session: AsyncSession,
    profile,
    pg_limits: dict | None,
    *,
    threshold: float | None = None,
) -> bool:
    """
    If any PG meter is at/above capacity_warn_pct and we have not warned yet,
    stamp capacity_warned_at (once). Returns True when a new warn was recorded.
    """
    if not profile or not pg_limits or not pg_limits.get("ready"):
        return False
    if getattr(profile, "capacity_warned_at", None) is not None:
        return False
    if threshold is None:
        from app.services.users import get_setting

        try:
            threshold = float(await get_setting(session, "capacity_warn_pct", "80") or 80)
        except Exception:
            threshold = 80.0
    meters = [pg_limits.get("users"), pg_limits.get("traffic")]
    if not capacity_should_warn(meters, float(threshold)):
        return False
    profile.capacity_warned_at = datetime.now(timezone.utc)
    try:
        await session.commit()
    except Exception:
        logger.debug("capacity_warned_at commit failed", exc_info=True)
        return False
    # The Telegram message is sent by the panel alert job (panel_alert_notifications).
    return True
