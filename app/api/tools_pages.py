"""The «ابزار و پاکسازی» area: its list page, and the owner's «سلامت سیستم» page with its re-run buttons."""

from __future__ import annotations

from collections.abc import Callable

from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.numbers import to_fa_digits
from app.runtime import BOOT_ID
from app.services.maintenance import (
    CLEAR_CACHES_PATH,
    RECONNECT_PATH,
    RESTART_PATH,
    RESYNC_PATH,
    clear_caches,
    maintenance_group,
    record_maintenance,
    request_panel_restart,
    restart_blocker,
    start_shop_bot_reconnect,
)
from app.services.service_resync import start_resync
from app.services.stuck_work import RERUN_PATH, request_rerun, stuck_group
from app.services.system_health import collect_health
from app.services.tools_nav import HEALTH_HREF, MAINTENANCE_HREF, TOOLS_LIST_HREF, tool_sections, tools_hub_sections


BOOT_ID_MAX = 64


def register_tools_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, require_admin: Callable, get_db: Callable
) -> None:
    @app.get(TOOLS_LIST_HREF, response_class=HTMLResponse)
    async def tools_list(request: Request, staff: dict = Depends(require_staff)) -> HTMLResponse:
        # Same visibility as the sidebar: a tool shows only when its own route would admit this staff member.
        sections = tool_sections(staff)
        if not sections:
            raise HTTPException(status_code=403, detail="دسترسی به ابزار و پاکسازی ندارید")
        return render(request, "tools.html", {"staff": staff, "tool_sections": sections, "hub": tools_hub_sections(sections)})

    @app.get(HEALTH_HREF, response_class=HTMLResponse)
    async def system_health_page(
        request: Request, staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> HTMLResponse:
        groups = (*await collect_health(session), await stuck_group(session))
        return render(request, "system_health.html", {"staff": staff, "groups": groups})

    @app.post(RERUN_PATH + "/{key}")
    async def system_health_rerun(
        key: str, staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        try:
            kind = await request_rerun(session, staff, key)
        except ValueError as exc:
            return RedirectResponse(f"{HEALTH_HREF}?err={quote(str(exc))}", status_code=303)
        done = f"«{kind.label}» همین حالا دوباره اجرا می‌شود؛ چند ثانیهٔ دیگر صفحه را تازه کنید"
        return RedirectResponse(f"{HEALTH_HREF}?ok={quote(done)}", status_code=303)

    @app.get(MAINTENANCE_HREF, response_class=HTMLResponse)
    async def maintenance_page(
        request: Request, staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> HTMLResponse:
        # The boot the page was restarted from: it waits until the panel answers from a newer one.
        restarting = (request.query_params.get("restarting") or "")[:BOOT_ID_MAX]
        return render(request, "maintenance.html", {
            "staff": staff, "groups": (await maintenance_group(session),), "restarting_from": restarting,
        })

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


def _back(path: str, **flash: str) -> RedirectResponse:
    key, value = next(iter(flash.items()))
    return RedirectResponse(f"{path}?{key}={quote(value)}", status_code=303)
