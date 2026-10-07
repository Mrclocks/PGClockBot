"""Admin backup / restore routes for the web panel."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.services.backup import (
    clear_idle_restore_status,
    create_backup,
    delete_backup,
    get_backup_path,
    resolve_stale_restore_status,
    save_uploaded_backup,
    start_restore_async,
)


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    return "application/json" in accept or xrw in {"xmlhttprequest", "fetch"}


def _status_payload() -> dict:
    st = resolve_stale_restore_status()
    try:
        from app.runtime import BOOT_ID, PID
        from app.services.updates import local_version

        st = dict(st)
        st["boot_id"] = BOOT_ID
        st["pid"] = PID
        st["current_version"] = local_version()
    except Exception:
        pass
    return st


def register_backup_pages(app, *, render, require_admin, get_db):
    @app.get("/settings/backup", response_class=HTMLResponse)
    async def backup_redirect(staff: dict = Depends(require_admin)):
        return RedirectResponse("/settings?tab=backup", status_code=303)

    @app.get("/backup/status")
    async def backup_status(staff: dict = Depends(require_admin)):
        """JSON progress for restore (and last known status) — polled by settings UI."""
        return JSONResponse(_status_payload())

    @app.post("/backup/clear")
    async def backup_clear(staff: dict = Depends(require_admin)):
        return JSONResponse({"ok": True, "status": clear_idle_restore_status()})

    @app.post("/backup/create")
    async def backup_create(
        staff: dict = Depends(require_admin),
        note: str = Form(""),
        include_env: str = Form(""),
        confirm_include_env: str = Form(""),
    ):
        want_env = str(include_env) in {"1", "on", "true", "yes"}
        confirmed = str(confirm_include_env) in {"1", "on", "true", "yes"}
        # Phase 2: .env is opt-in and requires an explicit second confirm field.
        if want_env and not confirmed:
            return RedirectResponse(
                "/settings?tab=backup&err="
                + quote(
                    "بکاپ با .env نیاز به تأیید امنیتی دوم دارد "
                    "(توکن‌ها و اسرار داخل فایل می‌روند)."
                ),
                status_code=303,
            )
        try:
            result = await asyncio.to_thread(
                create_backup,
                note=note,
                include_env=want_env and confirmed,
                created_by=f"web:{staff.get('username') or 'admin'}",
            )
        except Exception as e:
            from app.services.redact import user_safe_error

            return RedirectResponse(
                "/settings?tab=backup&err="
                + quote(
                    f"ساخت بکاپ ناموفق بود: {user_safe_error(e)}. "
                    "علت محتمل: فضای دیسک یا قفل فایل. "
                    "راه حل: فضای دیسک و دسترسی به data/backups را بررسی کنید و دوباره تلاش کنید."
                ),
                status_code=303,
            )
        env_note = (
            " — شامل .env (اسرار)"
            if result.get("include_env")
            else " — بدون .env"
        )
        return RedirectResponse(
            "/settings?tab=backup&ok="
            + quote(
                f"بکاپ ساخته شد: {result.get('filename')} "
                f"({result.get('size_human')}){env_note}"
            ),
            status_code=303,
        )

    @app.get("/backup/download/{backup_id}")
    async def backup_download(backup_id: str, staff: dict = Depends(require_admin)):
        path = get_backup_path(backup_id)
        if not path:
            return RedirectResponse(
                "/settings?tab=backup&err=" + quote("بکاپ یافت نشد"),
                status_code=303,
            )
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/zip",
        )

    @app.post("/backup/delete/{backup_id}")
    async def backup_delete(backup_id: str, staff: dict = Depends(require_admin)):
        ok = delete_backup(backup_id)
        if not ok:
            return RedirectResponse(
                "/settings?tab=backup&err=" + quote("حذف بکاپ ممکن نشد"),
                status_code=303,
            )
        return RedirectResponse(
            "/settings?tab=backup&ok=" + quote("بکاپ حذف شد"),
            status_code=303,
        )

    @app.post("/backup/restore/{backup_id}")
    async def backup_restore(
        request: Request,
        backup_id: str,
        staff: dict = Depends(require_admin),
        restore_env: str = Form(""),
        confirm: str = Form(""),
    ):
        confirm_ok = (confirm or "").strip().upper() in {"1", "YES", "ON", "TRUE", "RESTORE"}
        wants_json = _wants_json(request)

        def err(msg: str, *, code: int = 400, status=None):
            if wants_json:
                body = {"ok": False, "error": msg}
                if status is not None:
                    body["status"] = status
                return JSONResponse(body, status_code=code)
            return RedirectResponse(
                "/settings?tab=backup&err=" + quote(msg),
                status_code=303,
            )

        if not confirm_ok:
            return err("تأیید ریستور انجام نشد")
        path = get_backup_path(backup_id)
        if not path:
            return err("بکاپ یافت نشد", code=404)
        st = resolve_stale_restore_status()
        if st.get("state") == "running":
            return err("یک عملیات ریستور در حال اجراست", code=409, status=st)
        if st.get("awaiting_restart"):
            return err(
                "ریستور قبلی در انتظار راه‌اندازی مجدد است",
                code=409,
                status=st,
            )
        # Release DB connections before swapping the file
        from app.db.session import engine

        await engine.dispose()
        result = start_restore_async(
            path,
            restore_env=str(restore_env) in {"1", "on", "true", "yes"},
            safety_backup=True,
            restart=True,
            actor=f"web:{staff.get('username') or 'admin'}",
        )
        if not result.get("ok"):
            if wants_json:
                return JSONResponse(result, status_code=409)
            return err(result.get("error") or "شروع ریستور ممکن نشد", code=409)

        if wants_json:
            return JSONResponse(result)

        # Progressive enhancement: classic form POST — land on backup tab with ops UI.
        return RedirectResponse(
            "/settings?tab=backup&restore=1",
            status_code=303,
        )

    @app.post("/backup/upload")
    async def backup_upload(
        staff: dict = Depends(require_admin),
        file: UploadFile = File(...),
    ):
        max_bytes = 500 * 1024 * 1024
        raw = await file.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return RedirectResponse(
                "/settings?tab=backup&err=" + quote("حجم بکاپ بیش از ۵۰۰ مگابایت است"),
                status_code=303,
            )
        result = await asyncio.to_thread(
            save_uploaded_backup,
            raw,
            filename=file.filename or "",
        )
        if not result.get("ok"):
            return RedirectResponse(
                "/settings?tab=backup&err=" + quote(result.get("error") or "آپلود نامعتبر"),
                status_code=303,
            )
        return RedirectResponse(
            "/settings?tab=backup&ok="
            + quote(f"بکاپ آپلود شد: {result.get('filename')}"),
            status_code=303,
        )
