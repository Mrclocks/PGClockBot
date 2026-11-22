"""«تعمیر نصب»: install the version that is already installed again, after a full backup.

Files changed by hand or a half-finished update can leave the panel broken although it runs the newest code of
its channel. The repair runs the panel's own update (fetch the channel's code, reset the files, reinstall the
packages, restart) only when that update would install nothing newer, so it never upgrades by surprise; a newer
version belongs on the update page. A full backup comes first and the repair stops if it fails. All of it runs
in the background, one at a time, and refuses to start while an update or a restore runs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.backup import create_backup
from app.services.backup_postgres import BackupDatabaseError
from app.services.panel_update import start_update
from app.services.update_channel import get_update_channel, migration_preflight
from app.services.updates import check_github_update, local_version

logger = logging.getLogger(__name__)

CONFIRM_PHRASE = "تعمیر"
STEP_LABELS: dict[str, str] = {
    "check": "بررسی نسخهٔ کانال…",
    "backup": "گرفتن بکاپ کامل…",
    "update": "به آپدیت سپرده شد؛ پنل چند لحظهٔ دیگر دوباره روشن می‌شود",
}
NOT_DONE = "تعمیر نصب انجام نشد؛ جزئیات در لاگ سرور ثبت شد"


class RepairRefused(Exception):
    """A reason the repair stopped, already worded for the owner."""


@dataclass(slots=True)
class RepairRun:
    started_at: datetime
    step: str = "check"
    error: str | None = None
    finished_at: datetime | None = None


_task: asyncio.Task[None] | None = None
_last_run: RepairRun | None = None


def repair_running() -> bool:
    return _task is not None and not _task.done()


def last_repair() -> RepairRun | None:
    return _last_run


async def _assert_nothing_newer() -> None:
    channel = get_update_channel()
    info = await check_github_update(force=True, channel=channel)
    if not info.get("remote_version"):
        raise RepairRefused("نسخهٔ گیت‌هاب خوانده نشد؛ کمی بعد دوباره امتحان کنید")
    if info.get("update_available"):
        raise RepairRefused("نسخهٔ جدیدتری آماده است؛ آن را از صفحهٔ آپدیت نصب کنید")
    preflight = await migration_preflight(channel, force=True)
    if preflight.get("blocked"):
        raise RepairRefused(str(preflight.get("message") or "مایگریشن کانال با این دیتابیس سازگار نیست"))


async def _repair(run: RepairRun, created_by: str) -> None:
    try:
        await _assert_nothing_newer()
        run.step = "backup"
        await asyncio.to_thread(create_backup, note="پیش از تعمیر نصب", include_env=False, created_by=created_by)
        run.step = "update"
        started = await asyncio.to_thread(start_update, local_version())
        if not started.get("ok"):
            raise RepairRefused(str(started.get("error") or NOT_DONE))
    except RepairRefused as exc:
        run.error = str(exc)
    except BackupDatabaseError as exc:
        # Its message is already safe to show; the detail is in the log.
        run.error = f"بکاپ گرفته نشد و تعمیر شروع نشد: {exc}"
    except Exception:  # noqa: BLE001 - background task of last resort; the owner sees a safe message
        logger.exception("install repair failed at step=%s", run.step)
        run.error = NOT_DONE
    finally:
        run.finished_at = datetime.now(timezone.utc)


def start_repair(*, created_by: str) -> None:
    """Start the repair in the background; ValueError when one is already running."""
    global _task, _last_run
    if repair_running():
        raise ValueError("تعمیر نصب همین حالا در جریان است")
    _last_run = RepairRun(started_at=datetime.now(timezone.utc))
    _task = asyncio.create_task(_repair(_last_run, created_by), name="install-repair")
