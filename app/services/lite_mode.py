"""«حالت سبک»: one owner switch that runs a few housekeeping jobs less often, so a small server works less.

Only jobs whose lateness costs nothing are spaced out: panel alerts, the first-use reminder, the cleanups and the
bot profile photo. Payments, settlements, billing, renewals, deliveries, customer notices, campaigns, freezes,
transfers and backups keep their own timers. Every relaxed job still runs, so no feature stops. The switch is
saved in settings and applied again each time the scheduler starts.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.services.job_trigger import set_job_interval
from app.services.staff_audit import actor_from_staff, add_staff_action

logger = logging.getLogger(__name__)

LITE_MODE_KEY = "lite_mode_enabled"
# job id -> seconds between runs while lite mode is on; each is at least three times the job's own interval.
RELAXED_JOBS: dict[str, int] = {
    "panel_alert_notifications": 15 * 60,
    "first_use_nudge": 90 * 60,
    "service_cleanup": 2 * 3600,
    "chat_message_purge": 3 * 3600,
    "order_purge": 3 * 3600,
    "pending_order_expiry": 2 * 3600,
    "bot_profile_photo": 24 * 3600,
}


async def lite_mode_on(session: AsyncSession) -> bool:
    raw = await session.scalar(select(Setting.value).where(Setting.key == LITE_MODE_KEY).limit(1))
    return str(raw or "").strip() == "1"


def apply_lite_mode(enabled: bool) -> int:
    """Space the relaxed jobs out (or put them back); returns how many jobs the scheduler changed."""
    changed = 0
    for job_id, seconds in RELAXED_JOBS.items():
        changed += set_job_interval(job_id, seconds if enabled else None)
    return changed


async def set_lite_mode(session: AsyncSession, staff: dict, *, enabled: bool) -> int:
    """Save the switch, record who changed it, then apply it; returns how many jobs changed."""
    if type(enabled) is not bool:
        raise ValueError("مقدار حالت سبک نامعتبر است")
    value = "1" if enabled else "0"
    await session.execute(
        insert(Setting).values(key=LITE_MODE_KEY, value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    add_staff_action(session, actor_from_staff(staff, shop_id=None), action="maintenance_run", target_type="job",
                     target_id=None, reason="روشن کردن حالت سبک" if enabled else "خاموش کردن حالت سبک")
    await session.commit()
    return apply_lite_mode(enabled)
