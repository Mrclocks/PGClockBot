"""The owner's «نگهداری» tools: reconnect the shop bots and clear the panel's in-memory caches.

Reconnecting restarts every active shop bot through the bot manager (stop, then start with the saved token),
a few at a time in a background task, so the request returns at once and a second press while it runs is
refused. Clearing caches empties only data caches that are read again on the next use; the PasarGuard HTTP
clients are left alone, since dropping one would cut a request that is using it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerProfile
from app.db.session import SessionLocal
from app.runtime import BOOT_AT
from app.services.backup import restore_in_progress
from app.services.bot_overview import clear_bot_status_cache
from app.services.dashboard_trend import clear_trend_cache
from app.services.mobile_bar import invalidate_bar_cache
from app.services.numbers import to_fa_digits
from app.services.panel_update import update_in_progress
from app.services.panel_brand import invalidate_brand_cache
from app.services.panel_inbox import invalidate_inbox_sidebar_cache
from app.services.panel_sidebar_cache import clear_sidebar_counts
from app.services.pg_access import clear_platform_pg_capability_cache
from app.services.pg_read_cache import clear_store as clear_pg_read_store
from app.services.pg_read_cache import invalidate_capability_cache
from app.services.release_notes import clear_remote_notes_cache
from app.services.reseller_bots import clear_reseller_token_cache, get_reseller_bot_manager
from app.services.service_control import schedule_panel_restart
from app.services.service_import import clear_identity_cache
from app.services.service_resync import last_resync, resync_running
from app.services.staff_audit import actor_from_staff, add_staff_action
from app.services.system_health import NEUTRAL, OK, WARN, HealthGroup, HealthItem
from app.services.updates import clear_update_cache
from app.services.users import clear_settings_cache

logger = logging.getLogger(__name__)

MAINTENANCE_PATH = "/tools/maintenance"
RECONNECT_PATH = MAINTENANCE_PATH + "/reconnect-bots"
CLEAR_CACHES_PATH = MAINTENANCE_PATH + "/clear-caches"
RESTART_PATH = MAINTENANCE_PATH + "/restart"
RESYNC_PATH = MAINTENANCE_PATH + "/resync"
RESTART_DELAY_SECONDS = 2.5
RESTART_CONFIRM = ("پنل و ربات‌ها چند ثانیه قطع و دوباره روشن شوند؟ کاری که همین حالا در جریان است تمام نمی‌شود و "
                   "بعد از روشن شدن ادامه پیدا می‌کند.")
RESYNC_CONFIRM = ("حجم و انقضای همهٔ سرویس‌ها دوباره از پاسارگارد خوانده شود؟ در پس‌زمینه انجام می‌شود و برای هر "
                  "صد سرویس یک درخواست به پنل می‌رود.")
RECONNECT_CONCURRENCY = 4
RECONNECT_CONFIRM = ("ربات همهٔ فروشگاه‌ها یکی‌یکی قطع و دوباره وصل شود؟ پیام‌های همین چند ثانیه بعد از وصل شدن "
                     "پاسخ می‌گیرند.")
CLEAR_CACHES_CONFIRM = "کش‌های پنل پاک شود؟ صفحه‌های بعدی چند لحظه کندتر باز می‌شوند و داده‌ها تازه خوانده می‌شوند."

# Data caches only: each is read again on its next use.
CACHE_CLEARERS: tuple[Callable[[], None], ...] = (
    clear_settings_cache,
    invalidate_brand_cache,
    invalidate_bar_cache,
    invalidate_inbox_sidebar_cache,
    clear_sidebar_counts,
    clear_trend_cache,
    clear_pg_read_store,
    invalidate_capability_cache,
    clear_platform_pg_capability_cache,
    clear_reseller_token_cache,
    clear_identity_cache,
    clear_bot_status_cache,
    clear_update_cache,
    clear_remote_notes_cache,
)


@dataclass(slots=True)
class ReconnectRun:
    started_at: datetime
    total: int = 0
    done: int = 0
    failed: int = 0
    finished_at: datetime | None = None


_reconnect_task: asyncio.Task[None] | None = None
_last_run: ReconnectRun | None = None


def _active_shops_query():
    return select(ResellerProfile.id).where(
        ResellerProfile.is_active.is_(True),
        ResellerProfile.setup_completed_at.is_not(None),
        ResellerProfile.bot_token.is_not(None),
    )


def reconnect_running() -> bool:
    return _reconnect_task is not None and not _reconnect_task.done()


async def _restart_all(run: ReconnectRun) -> None:
    manager = get_reseller_bot_manager()
    async with SessionLocal() as session:
        profile_ids = [int(pid) for pid in await session.scalars(_active_shops_query().order_by(ResellerProfile.id))]
    run.total = len(profile_ids)
    gate = asyncio.Semaphore(RECONNECT_CONCURRENCY)

    async def restart(profile_id: int) -> None:
        async with gate:
            try:
                started = await manager.restart_reseller_profile(profile_id)
            except Exception:  # noqa: BLE001 - one shop's bot must not stop the others from reconnecting
                logger.warning("shop bot reconnect failed profile=%s", profile_id, exc_info=True)
                started = False
            run.done += 1
            run.failed += 0 if started else 1

    await asyncio.gather(*(restart(pid) for pid in profile_ids))
    run.finished_at = datetime.now(timezone.utc)
    logger.info("shop bots reconnected total=%s failed=%s", run.total, run.failed)


def start_shop_bot_reconnect() -> None:
    """Start reconnecting every active shop bot in the background; ValueError when it cannot start."""
    global _reconnect_task, _last_run
    if get_reseller_bot_manager() is None:
        raise ValueError("ربات فروشگاه‌ها در این پنل اجرا نمی‌شوند")
    if reconnect_running():
        raise ValueError("اتصال دوبارهٔ ربات‌ها همین حالا در جریان است")
    _last_run = ReconnectRun(started_at=datetime.now(timezone.utc))
    _reconnect_task = asyncio.create_task(_restart_all(_last_run), name="shop-bots-reconnect")


def clear_caches() -> int:
    """Empty every data cache; returns how many were cleared (a failing one is logged and skipped)."""
    cleared = 0
    for clear in CACHE_CLEARERS:
        try:
            clear()
            cleared += 1
        except Exception:  # noqa: BLE001 - a cache that cannot be cleared expires on its own timer
            logger.warning("cache clear failed cache=%s", getattr(clear, "__name__", clear), exc_info=True)
    return cleared


async def record_maintenance(session: AsyncSession, staff: dict, *, label: str) -> None:
    """Write the tool's use to the staff report."""
    add_staff_action(session, actor_from_staff(staff, shop_id=None), action="maintenance_run",
                     target_type="job", target_id=None, reason=label)
    await session.commit()


def _bots_summary(running: int, active: int) -> tuple[str, str]:
    run = _last_run
    if reconnect_running() and run is not None:
        return NEUTRAL, f"در حال اتصال دوباره: {to_fa_digits(run.done)} از {to_fa_digits(run.total)}"
    tone = OK if running >= active else WARN
    summary = f"{to_fa_digits(running)} از {to_fa_digits(active)} ربات در حال اجرا"
    if run is not None and run.finished_at and run.failed:
        summary += f"؛ آخرین بار {to_fa_digits(run.failed)} ربات وصل نشد"
    return tone, summary


async def restart_blocker() -> str | None:
    """Why the panel must not restart now, or None."""
    if await asyncio.to_thread(update_in_progress):
        return "آپدیت پنل در جریان است؛ بعد از پایان آن دوباره امتحان کنید"
    if await asyncio.to_thread(restore_in_progress):
        return "بازگردانی بکاپ در جریان است؛ بعد از پایان آن دوباره امتحان کنید"
    return None


async def request_panel_restart() -> None:
    """Schedule one restart of the whole panel; ValueError when it must not or cannot happen now."""
    blocker = await restart_blocker()
    if blocker:
        raise ValueError(blocker)
    if not await asyncio.to_thread(
        schedule_panel_restart, delay_sec=RESTART_DELAY_SECONDS, reason="owner restart from maintenance tools"
    ):
        raise ValueError("راه‌اندازی مجدد زمان‌بندی نشد؛ شاید یکی همین حالا در جریان است")


def _uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{to_fa_digits(max(minutes, 1))} دقیقه"
    hours = minutes // 60
    return f"{to_fa_digits(hours)} ساعت" if hours < 48 else f"{to_fa_digits(hours // 24)} روز"


async def _restart_line() -> HealthItem:
    blocker = await restart_blocker()
    summary = blocker or f"روشن از {_uptime(time.time() - BOOT_AT)} پیش"
    return HealthItem("restart", "راه‌اندازی مجدد پنل", "refresh", WARN if blocker else "", summary,
                      action="" if blocker else RESTART_PATH, confirm=RESTART_CONFIRM,
                      confirm_title="راه‌اندازی مجدد پنل")


def _resync_line() -> HealthItem:
    run = last_resync()
    if resync_running() and run is not None:
        summary = f"در حال خواندن: {to_fa_digits(run.read)} سرویس"
        return HealthItem("resync", "همگام‌سازی با پنل", "connection", NEUTRAL, summary)
    summary = "حجم و انقضای همهٔ سرویس‌ها دوباره از پاسارگارد"
    tone = ""
    if run is not None and run.finished_at:
        summary = f"آخرین بار {to_fa_digits(run.synced)} سرویس به‌روز شد"
        if run.unread or run.failed_pages:
            summary += f"؛ {to_fa_digits(run.unread)} خوانده نشد"
            tone = WARN
    return HealthItem("resync", "همگام‌سازی با پنل", "connection", tone, summary, action=RESYNC_PATH,
                      confirm=RESYNC_CONFIRM, confirm_title="همگام‌سازی با پنل")


async def maintenance_group(session: AsyncSession) -> HealthGroup:
    """The tools of the page, each with what it acts on now."""
    manager = get_reseller_bot_manager()
    active = int(await session.scalar(select(func.count()).select_from(_active_shops_query().subquery())) or 0)
    if manager is None:
        bots = HealthItem("bots", "ربات فروشگاه‌ها", "bot", NEUTRAL, "در این پنل اجرا نمی‌شوند")
    else:
        tone, summary = _bots_summary(manager.running_count(), active)
        action = "" if reconnect_running() or not active else RECONNECT_PATH
        bots = HealthItem("bots", "ربات فروشگاه‌ها", "bot", tone, summary, action=action, confirm=RECONNECT_CONFIRM,
                           confirm_title="اتصال دوبارهٔ ربات‌ها")
    caches = HealthItem("caches", "کش‌های پنل", "refresh", "", "تنظیمات، منو، ظاهر و داده‌های موقت پاسارگارد",
                        action=CLEAR_CACHES_PATH, confirm=CLEAR_CACHES_CONFIRM,
                        confirm_title="پاک کردن کش‌ها")
    return HealthGroup("نگهداری", (bots, caches, _resync_line(), await _restart_line()))
