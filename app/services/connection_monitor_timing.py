"""How often connection monitoring checks its targets, and how long a result stays fresh.

The status rules count rounds: three failed rounds mean ``down`` and two successful rounds
mean recovered. Results stay fresh for three rounds, never less than the contract's 180 s,
so the rules mean the same at every interval. The default (60 s) keeps the original
contract exactly. A longer interval only spaces the rounds out: it starts the xray core and
reads the panel catalog less often.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.services.connection_monitor_access import MonitorAccess, require_monitor_owner
from app.services.connection_status_types import FRESHNESS

INTERVAL_SETTING_KEY = "connection_monitor_interval_seconds"
DEFAULT_INTERVAL_SECONDS = 60
# Seconds -> label on the settings form.
INTERVAL_CHOICES: dict[int, str] = {
    60: "هر ۱ دقیقه",
    120: "هر ۲ دقیقه",
    300: "هر ۵ دقیقه",
    600: "هر ۱۰ دقیقه",
    900: "هر ۱۵ دقیقه",
}
FRESH_ROUNDS = 3
INVALID_INTERVAL = "فاصلهٔ بررسی نامعتبر است."


@dataclass(frozen=True, slots=True)
class MonitorTiming:
    interval: timedelta
    freshness: timedelta

    @property
    def interval_seconds(self) -> int:
        return int(self.interval.total_seconds())


def timing_for(seconds: int) -> MonitorTiming:
    interval = timedelta(seconds=seconds)
    return MonitorTiming(interval=interval, freshness=max(FRESHNESS, interval * FRESH_ROUNDS))


DEFAULT_TIMING = timing_for(DEFAULT_INTERVAL_SECONDS)


def parse_interval(raw: object) -> int:
    """A stored or submitted interval; anything outside the choices reads as the default."""
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return seconds if seconds in INTERVAL_CHOICES else DEFAULT_INTERVAL_SECONDS


async def monitor_timing(session: AsyncSession) -> MonitorTiming:
    raw = await session.scalar(select(Setting.value).where(Setting.key == INTERVAL_SETTING_KEY).limit(1))
    return timing_for(parse_interval(raw))


async def set_monitor_interval(session: AsyncSession, access: MonitorAccess, *, seconds: int) -> MonitorTiming:
    """Save the interval; only the monitor's owner may, and only one of the offered choices."""
    if type(seconds) is not int or seconds not in INTERVAL_CHOICES:
        raise ValueError(INVALID_INTERVAL)
    await require_monitor_owner(session, access)
    await session.execute(
        insert(Setting)
        .values(key=INTERVAL_SETTING_KEY, value=str(seconds))
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": str(seconds)})
    )
    await session.commit()
    return timing_for(seconds)
