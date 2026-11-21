"""Panel self-update routes: the update tab redirect, status polling, clearing a stuck run,
choosing the update channel and starting an update.

Registered from ``create_api_app`` at the point its routes always had, so route order is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)


def register_update_routes(  # noqa: C901 - counts the nested routes; each is listed on its own
    app: object,
    *,
    require_admin: Callable[..., object],
) -> None:
    @app.get("/update", response_class=HTMLResponse)
    async def update_page(
        request: Request,
        staff: dict = Depends(require_admin),
    ):
        q = "tab=update"
        if request.query_params.get("force") == "1":
            q += "&force=1"
        return RedirectResponse(f"/settings?{q}", status_code=303)

    @app.get("/update/status")
    async def update_status(staff: dict = Depends(require_admin)):
        from app.runtime import BOOT_ID, PID
        from app.services.panel_update import resolve_stale_update_status
        from app.services.updates import local_version

        st = resolve_stale_update_status()
        st["current_version"] = local_version()
        st["boot_id"] = BOOT_ID
        st["pid"] = PID
        return st

    @app.post("/update/clear")
    async def update_clear(staff: dict = Depends(require_admin)):
        """Admin escape hatch: unlock stuck update UI."""
        from app.services.panel_update import clear_idle_status, resolve_stale_update_status

        st = resolve_stale_update_status()
        if st.get("state") == "running":
            # Still try to clear zombies
            from app.services.panel_update import _thread_alive

            if _thread_alive():
                return {"ok": False, "error": "عملیات در حال اجراست", "status": st}
        return {"ok": True, "status": clear_idle_status()}

    @app.post("/update/channel")
    async def update_channel_set(
        request: Request,
        staff: dict = Depends(require_admin),
    ):
        """Persist UPDATE_CHANNEL (main|dev) and return fresh update-tab context."""
        from app.services.panel_update import update_page_context
        from app.services.update_channel import normalize_channel, set_update_channel
        from app.services.updates import clear_update_cache

        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = (body or {}).get("channel")
        if raw is None or str(raw).strip() == "":
            return {"ok": False, "error": "کانال آپدیت مشخص نشده"}
        channel = set_update_channel(raw)
        clear_update_cache()
        # Always re-check GitHub for the newly selected channel tip.
        # Header badge stays on DEPLOYED_CHANNEL until an update is applied.
        ctx = await update_page_context(force_check=True)
        return {
            "ok": True,
            "channel": channel,
            "channel_label": ctx.get("update_channel_label"),
            "deployed_channel": ctx.get("deployed_channel"),
            "deployed_channel_label": ctx.get("deployed_channel_label"),
            "channels": ctx.get("update_channels"),
            "update_info": ctx.get("update_info"),
            "migration_preflight": ctx.get("migration_preflight"),
            "can_start_update": ctx.get("can_start_update"),
            "local_version": ctx.get("local_version"),
            "changelog": ctx.get("changelog"),
            "channel_check_message": ctx.get("channel_check_message"),
            "channel_check_tone": ctx.get("channel_check_tone"),
            "normalized": normalize_channel(raw),
        }

    @app.post("/update/start")
    async def update_start(
        request: Request,
        staff: dict = Depends(require_admin),
    ):
        from app.services.panel_update import clear_idle_status, start_update
        from app.services.update_channel import get_update_channel, migration_preflight
        from app.services.updates import check_github_update, clear_update_cache, is_newer

        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        clear_update_cache()
        channel = get_update_channel()
        info = await check_github_update(force=True, channel=channel)
        target = (body or {}).get("target") or info.get("remote_version")
        force = bool((body or {}).get("force"))
        preflight = await migration_preflight(channel, force=True)
        if preflight.get("blocked"):
            return {
                "ok": False,
                "error": preflight.get("message") or "مایگریشن کانال مقصد ناسازگار است",
                "info": info,
                "migration_preflight": preflight,
            }
        # Retry after error may force; otherwise require a real newer remote.
        if not info.get("update_available") and not force:
            from app.services.panel_update import read_status

            st = read_status()
            if st.get("state") != "error" and not st.get("restart_required"):
                return {"ok": False, "error": "نسخه جدیدی برای آپدیت نیست", "info": info}
        if target and info.get("remote_version") and not force:
            # Prefer the freshly checked remote over a stale client target.
            if is_newer(info.get("remote_version"), target):
                target = info.get("remote_version")
        # Clear leftover awaiting before starting a forced retry.
        if force:
            clear_idle_status()
        return start_update(target_version=target)
