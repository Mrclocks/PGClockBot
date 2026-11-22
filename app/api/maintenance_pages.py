"""The owner's «نگهداری سرور» page (``/tools/maintenance``) and its tools: reconnect the shop bots, clear the
caches, re-sync services from PasarGuard, restart the panel and repair the installation."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.runtime import BOOT_ID
from app.services.install_repair import CONFIRM_PHRASE, start_repair
from app.services.lite_mode import set_lite_mode
from app.services.maintenance import (
    CLEAR_CACHES_PATH,
    LITE_OFF_PATH,
    LITE_ON_PATH,
    RECONNECT_PATH,
    REPAIR_PATH,
    RESTART_PATH,
    RESYNC_PATH,
    clear_caches,
    maintenance_group,
    record_maintenance,
    request_panel_restart,
    restart_blocker,
    start_shop_bot_reconnect,
)
from app.services.numbers import to_fa_digits
from app.services.service_resync import start_resync
from app.services.tools_nav import MAINTENANCE_HREF

BOOT_ID_MAX = 64


def register_maintenance_pages(
    app: FastAPI, *, render: Callable, require_admin: Callable, get_db: Callable
) -> None:
    @app.get(MAINTENANCE_HREF, response_class=HTMLResponse)
    async def maintenance_page(
        request: Request, staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> HTMLResponse:
        # The boot the page was restarted from: it waits until the panel answers from a newer one.
        restarting = (request.query_params.get("restarting") or "")[:BOOT_ID_MAX]
        return render(request, "maintenance.html", {
            "staff": staff, "groups": (await maintenance_group(session),), "restarting_from": restarting,
        })

    _register_lite_mode(app, require_admin=require_admin, get_db=get_db)

    @app.post(RECONNECT_PATH)
    async def maintenance_reconnect_bots(
        staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        try:
            start_shop_bot_reconnect()
        except ValueError as exc:
            return _back(MAINTENANCE_HREF, err=str(exc))
        await record_maintenance(session, staff, label="اتصال دوبارهٔ ربات فروشگاه‌ها")
        return _back(MAINTENANCE_HREF, ok="اتصال دوبارهٔ ربات‌ها شروع شد؛ چند ثانیهٔ دیگر صفحه را تازه کنید")

    @app.post(CLEAR_CACHES_PATH)
    async def maintenance_clear_caches(
        staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        cleared = clear_caches()
        await record_maintenance(session, staff, label="پاک کردن کش‌های پنل")
        return _back(MAINTENANCE_HREF, ok=f"کش‌های پنل پاک شد ({to_fa_digits(cleared)} مورد)")

    @app.post(RESTART_PATH)
    async def maintenance_restart(
        staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        blocker = await restart_blocker()
        if blocker:
            return _back(MAINTENANCE_HREF, err=blocker)
        # Recorded before the restart is scheduled: the process that answers this request is about to stop.
        await record_maintenance(session, staff, label="درخواست راه‌اندازی مجدد پنل")
        try:
            await request_panel_restart()
        except ValueError as exc:
            return _back(MAINTENANCE_HREF, err=str(exc))
        return RedirectResponse(f"{MAINTENANCE_HREF}?restarting={BOOT_ID}", status_code=303)

    @app.post(RESYNC_PATH)
    async def maintenance_resync(
        staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        try:
            start_resync()
        except ValueError as exc:
            return _back(MAINTENANCE_HREF, err=str(exc))
        await record_maintenance(session, staff, label="همگام‌سازی سرویس‌ها با پنل")
        return _back(MAINTENANCE_HREF, ok="همگام‌سازی با پنل شروع شد؛ چند لحظهٔ دیگر صفحه را تازه کنید")


    @app.post(REPAIR_PATH)
    async def maintenance_repair(
        request: Request, staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        form = await request.form()
        if str(form.get("confirm_phrase") or "").strip() != CONFIRM_PHRASE:
            return _back(MAINTENANCE_HREF, err=f"برای تعمیر نصب باید «{CONFIRM_PHRASE}» را بنویسید")
        blocker = await restart_blocker()
        if blocker:
            return _back(MAINTENANCE_HREF, err=blocker)
        try:
            start_repair(created_by=str(staff.get("username") or "owner"))
        except ValueError as exc:
            return _back(MAINTENANCE_HREF, err=str(exc))
        await record_maintenance(session, staff, label="تعمیر نصب")
        return _back(MAINTENANCE_HREF, ok="تعمیر نصب شروع شد: اول بکاپ گرفته می‌شود؛ چند لحظهٔ دیگر صفحه را تازه کنید")


def _register_lite_mode(app: FastAPI, *, require_admin: Callable, get_db: Callable) -> None:
    """One path per state, so a second press (or a double click) leaves the switch where it is."""
    @app.post(LITE_ON_PATH)
    async def maintenance_lite_on(
        staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        await set_lite_mode(session, staff, enabled=True)
        return _back(MAINTENANCE_HREF, ok="حالت سبک روشن شد؛ کارهای جانبی از این به بعد کمتر اجرا می‌شوند")

    @app.post(LITE_OFF_PATH)
    async def maintenance_lite_off(
        staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        await set_lite_mode(session, staff, enabled=False)
        return _back(MAINTENANCE_HREF, ok="حالت سبک خاموش شد؛ همهٔ کارها با فاصلهٔ عادی اجرا می‌شوند")


def _back(path: str, **flash: str) -> RedirectResponse:
    key, value = next(iter(flash.items()))
    return RedirectResponse(f"{path}?{key}={quote(value)}", status_code=303)
