"""The owner's «سلامت سیستم» page: one status line per part of the server, read from what is already measured.

Every check reads state the panel keeps anyway (the PasarGuard link watchdog, the cached bot probe, the job
metrics, files on disk, one tiny database query); none adds a call to PasarGuard. Blocking work (disk usage,
the backup folder, the certificate, the migration scripts) runs in a thread. A check that fails is shown as
«بررسی نشد» with its detail in the server log, so one broken part never blanks the page.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, get_settings
from app.db.alembic_runner import heads as migration_heads
from app.services import pg_link_health
from app.services.backup import BACKUP_DIR, BACKUP_PREFIX
from app.services.bot_overview import bot_status
from app.services.db_safe import rollback_quiet
from app.services.job_metrics import JobStatsView, job_metrics
from app.services.numbers import to_fa_digits
from app.services.resource_cost import JOB_LABELS
from app.services.ssl_certs import cert_status
from app.services.updates import local_version

logger = logging.getLogger(__name__)

OK, WARN, ERR, NEUTRAL = "ok", "warn", "err", "neutral"
TONE_LABELS: dict[str, str] = {OK: "عادی", WARN: "هشدار", ERR: "خطا", NEUTRAL: "بدون داده"}
BADGE_CLASSES: dict[str, str] = {OK: "ok", WARN: "warn", ERR: "error", NEUTRAL: "neutral"}

DISK_ERR_GIB = 1.0
DISK_WARN_GIB = 3.0
BACKUP_STALE = timedelta(days=2)
CERT_WARN = timedelta(days=14)
LINK_STALE_SECONDS = 300
NAMED_FAILING_JOBS = 2
NOT_CHECKED = "بررسی نشد"

# key -> (label, icon from _icons.html), in page order.
CHECKS: dict[str, tuple[str, str]] = {
    "database": ("دیتابیس", "server"),
    "disk": ("فضای دیسک", "monitor"),
    "version": ("نسخهٔ پنل", "refresh"),
    "pasarguard": ("اتصال پاسارگارد", "connection"),
    "bot": ("ربات تلگرام", "bot"),
    "ssl": ("گواهی SSL", "lock"),
    "backup": ("آخرین بکاپ", "download"),
    "jobs": ("کارهای پس‌زمینه", "pulse"),
}


@dataclass(frozen=True, slots=True)
class HealthItem:
    key: str
    label: str
    icon: str
    tone: str
    summary: str
    href: str = ""
    # A POST path that fixes the line (behind a confirm dialog); a line has a link or an action, not both.
    action: str = ""
    # The confirm dialog's question for ``action``; empty uses the re-run wording.
    confirm: str = ""
    confirm_title: str = ""

    @property
    def tone_label(self) -> str:
        """Empty for a tool line without a state (``tone=""``), which shows no badge."""
        return TONE_LABELS.get(self.tone, "")

    @property
    def badge_class(self) -> str:
        """The panel's badge class for the tone (the red badge is named ``error``)."""
        return BADGE_CLASSES.get(self.tone, "")


@dataclass(frozen=True, slots=True)
class HealthGroup:
    label: str
    items: tuple[HealthItem, ...]


def _item(key: str, tone: str, summary: str, href: str = "") -> HealthItem:
    label, icon = CHECKS[key]
    return HealthItem(key, label, icon, tone, summary, href)


def _fa(value: object) -> str:
    return to_fa_digits(str(value))


async def check_database(session: AsyncSession) -> HealthItem:
    started = time.monotonic()
    await session.execute(text("SELECT 1"))
    elapsed_ms = round((time.monotonic() - started) * 1000)
    current = set((await session.scalars(text("SELECT version_num FROM alembic_version"))).all())
    expected = set(await asyncio.to_thread(_code_heads))
    if current != expected:
        return _item("database", WARN,
                          "ساختار دیتابیس با نسخهٔ برنامه یکی نیست؛ مایگریشن را اجرا کنید", "/settings?tab=update")
    speed = f"{_fa(elapsed_ms)} میلی‌ثانیه" if elapsed_ms >= 1 else "کمتر از ۱ میلی‌ثانیه"
    return _item("database", OK, f"پاسخ در {speed}؛ ساختار به‌روز")


@functools.cache
def _code_heads() -> tuple[str, ...]:
    # The migration scripts only change with a new release, which restarts the process.
    return tuple(migration_heads())


async def check_disk() -> HealthItem:
    usage = await asyncio.to_thread(shutil.disk_usage, DATA_DIR)
    free_gib = usage.free / 1024**3
    tone = ERR if free_gib < DISK_ERR_GIB else WARN if free_gib < DISK_WARN_GIB else OK
    return _item("disk", tone, f"{_fa(f'{free_gib:.1f}')} گیگابایت آزاد")


async def check_version() -> HealthItem:
    return _item("version", OK, f"v{local_version()}", "/settings?tab=update")


async def check_pasarguard() -> HealthItem:
    link = pg_link_health.snapshot()
    last_ok = _parse_iso(link.get("last_ok_at"))
    recent = int(link.get("recent_failure_count") or 0)
    href = "/settings?tab=bot"
    if last_ok is None:
        return _item("pasarguard", NEUTRAL, "هنوز درخواستی به پنل نرفته", href)
    stale = (datetime.now(timezone.utc) - last_ok).total_seconds() > LINK_STALE_SECONDS
    if stale and recent:
        return _item("pasarguard", ERR, "پنل چند دقیقه است پاسخ نمی‌دهد", href)
    if recent:
        return _item("pasarguard", WARN,
                          f"{_fa(recent)} خطا در یک ساعت اخیر", href)
    return _item("pasarguard", OK, "پاسخ‌ها عادی است", href)


async def check_bot() -> HealthItem:
    status = await bot_status(get_settings().bot_token)
    tone = {"ok": OK, "err": ERR}.get(str(status.get("state")), NEUTRAL)
    username = status.get("username")
    summary = f"@{username} آنلاین" if tone == OK and username else str(status.get("detail") or NOT_CHECKED)
    return _item("bot", tone, summary, "/settings?tab=bot")


async def check_certificate() -> HealthItem:
    status = await asyncio.to_thread(cert_status)
    href = "/settings?tab=ssl"
    if not status.get("ssl_enabled"):
        return _item("ssl", NEUTRAL, "وب‌پنل روی HTTPS نیست", href)
    expires = _parse_iso(status.get("expires_at"))
    if expires is None:
        return _item("ssl", WARN, "تاریخ انقضای گواهی خوانده نشد", href)
    left = expires - datetime.now(timezone.utc)
    if left <= timedelta(0):
        return _item("ssl", ERR, "گواهی منقضی شده است", href)
    tone = WARN if left < CERT_WARN else OK
    return _item("ssl", tone, f"{_fa(left.days)} روز تا انقضا", href)


async def check_backup() -> HealthItem:
    newest = await asyncio.to_thread(_newest_backup_time)
    href = "/settings?tab=backup"
    if newest is None:
        return _item("backup", WARN, "هنوز بکاپی ساخته نشده", href)
    age = datetime.now(timezone.utc) - newest
    hours = int(age.total_seconds() // 3600)
    when = f"{_fa(hours)} ساعت پیش" if hours < 48 else f"{_fa(age.days)} روز پیش"
    return _item("backup", WARN if age > BACKUP_STALE else OK, when, href)


def _newest_backup_time() -> datetime | None:
    if not BACKUP_DIR.is_dir():
        return None
    times = [path.stat().st_mtime for path in BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.zip")]
    return datetime.fromtimestamp(max(times), timezone.utc) if times else None


async def check_jobs() -> HealthItem:
    views = job_metrics.snapshot()
    href = "/dashboard"
    if not views:
        return _item("jobs", NEUTRAL, "هنوز کاری اجرا نشده", href)
    failing = [view for view in views if _last_run_failed(view)]
    if not failing:
        return _item("jobs", OK, f"{_fa(len(views))} کار، آخرین اجرا بدون خطا", href)
    names = "، ".join(JOB_LABELS.get(view.job_id, view.job_id) for view in failing[:NAMED_FAILING_JOBS])
    more = f" و {_fa(len(failing) - NAMED_FAILING_JOBS)} کار دیگر" if len(failing) > NAMED_FAILING_JOBS else ""
    return _item("jobs", ERR, f"آخرین اجرا با خطا: {names}{more}", href)


def _last_run_failed(view: JobStatsView) -> bool:
    if view.last_failure_at is None:
        return False
    return view.last_success_at is None or view.last_failure_at >= view.last_success_at


def _parse_iso(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _guarded(key: str, check: Callable[[], Awaitable[HealthItem]]) -> HealthItem:
    try:
        return await check()
    except Exception:  # noqa: BLE001 - read-only status line; the rest of the page must still render
        logger.warning("system health check failed check=%s", key, exc_info=True)
        return _item(key, NEUTRAL, NOT_CHECKED)


async def _database(session: AsyncSession) -> HealthItem:
    try:
        return await check_database(session)
    except Exception:  # noqa: BLE001 - read-only status line; the session is rolled back for the page
        logger.warning("system health check failed check=database", exc_info=True)
        await rollback_quiet(session)
        return _item("database", ERR, "دیتابیس پاسخ نداد")


async def collect_health(session: AsyncSession) -> tuple[HealthGroup, ...]:
    """All status lines, grouped as the page shows them."""
    database = await _database(session)
    disk, version, pasarguard, bot, ssl, backup, jobs = await asyncio.gather(
        _guarded("disk", check_disk),
        _guarded("version", check_version),
        _guarded("pasarguard", check_pasarguard),
        _guarded("bot", check_bot),
        _guarded("ssl", check_certificate),
        _guarded("backup", check_backup),
        _guarded("jobs", check_jobs),
    )
    return (
        HealthGroup("سرور", (database, disk, version)),
        HealthGroup("اتصال‌ها", (pasarguard, bot, ssl)),
        HealthGroup("نگهداری", (backup, jobs)),
    )
