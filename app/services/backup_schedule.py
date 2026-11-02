"""Nightly automatic backup: build, check, apply retention, deliver, and report problems.

The archive is built and checked in a worker thread; ``pg_dump`` can take
minutes and must not freeze the bot and the web panel meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.backup import create_backup, prune_backups, validate_backup_archive
from app.services.backup_settings import KEEP_KEY, keep_count
from app.services.backup_telegram import notify_backup_problem, send_backup
from app.services.users import get_setting, on, set_setting

logger = logging.getLogger(__name__)

ENABLED_KEY = "backup_schedule_enabled"
HOUR_KEY = "backup_schedule_hour"
INCLUDE_ENV_KEY = "backup_include_env_scheduled"
LAST_OK_KEY = "backup_last_ok_at"
LAST_ATTEMPT_KEY = "backup_last_attempt_at"
VERIFY_KEY = "backup_last_verify_ok"
DEFAULT_HOUR = 3

FAILED_MESSAGE = (
    "⚠️ بکاپ خودکار PGClockBot ساخته نشد. از وب‌پنل ← تنظیمات ← بکاپ یک بکاپ دستی بسازید "
    "و اگر دوباره خطا داد، لاگ سرور را بررسی کنید."
)
UNVERIFIED_MESSAGE = (
    "⚠️ بکاپ خودکار PGClockBot ساخته شد ولی بررسی سلامت آن رد شد. "
    "از وب‌پنل ← تنظیمات ← بکاپ یک بکاپ دستی بسازید."
)


def _scheduled_hour(raw: object) -> int:
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_HOUR
    return max(0, min(23, hour))


async def _is_due(session: AsyncSession, now: datetime) -> bool:
    if not on(await get_setting(session, ENABLED_KEY, "1")):
        return False
    if now.hour != _scheduled_hour(await get_setting(session, HOUR_KEY, str(DEFAULT_HOUR))):
        return False
    today = now.strftime("%Y-%m-%d")
    for key in (LAST_ATTEMPT_KEY, LAST_OK_KEY):
        if ((await get_setting(session, key, "")) or "").startswith(today):
            return False
    return True


async def run_nightly_backup(session: AsyncSession, now: datetime) -> str:
    """One automatic backup per UTC day; returns skipped, ok, unverified or failed."""
    if not await _is_due(session, now):
        return "skipped"
    # Claim the day before the long work, so the next tick does not start a second run.
    await set_setting(session, LAST_ATTEMPT_KEY, now.isoformat())
    await session.commit()
    include_env = on(await get_setting(session, INCLUDE_ENV_KEY, "0"))
    try:
        meta = await asyncio.to_thread(create_backup, note="scheduled", include_env=include_env, created_by="scheduler")
    except Exception:  # noqa: BLE001 - any build failure is reported; nothing was changed
        logger.exception("scheduled backup failed")
        await notify_backup_problem(session, FAILED_MESSAGE)
        return "failed"
    path = Path(str(meta.get("path") or ""))
    verified, error, _manifest = await asyncio.to_thread(validate_backup_archive, path)
    await set_setting(session, VERIFY_KEY, "1" if verified else "0")
    if verified:
        await set_setting(session, LAST_OK_KEY, now.isoformat())
    await session.commit()
    await asyncio.to_thread(prune_backups, keep_count(await get_setting(session, KEEP_KEY, "10")))
    if not verified:
        logger.warning("scheduled backup verify failed: %s", error)
        await notify_backup_problem(session, UNVERIFIED_MESSAGE)
        return "unverified"
    try:
        await send_backup(
            session, path, include_env=bool(meta.get("include_env")),
            caption=f"💾 بکاپ خودکار PGClockBot — {now.strftime('%Y-%m-%d %H:%M')} UTC",
        )
    except Exception:  # noqa: BLE001 - a failed delivery must not undo a good backup
        logger.exception("scheduled backup telegram delivery failed")
    logger.info("scheduled backup ok id=%s", meta.get("id"))
    return "ok"
