"""Web panel: upload, order, preview and remove the photos and videos of connection guides.

The platform owner manages the platform guides under ``/settings``; a shop manages only its own
guides under ``/shop-settings``. The shop comes from the authenticated session, never the form.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.services.connection_guides import (
    add_guide_media,
    get_connection_guides,
    guide_media_files,
    move_guide_media,
    remove_guide_media,
)
from app.services.guide_media import (
    MAX_VIDEO_BYTES,
    GuideMediaError,
    check_upload,
    delete_media_file,
    is_media_missing,
    media_path,
    write_media_file,
)
from app.services.shop_scope import is_platform_admin, shop_owner_id


PLATFORM_BASE = "/settings/guides/media"
SHOP_BASE = "/shop-settings/guides/media"
_MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "mp4": "video/mp4"}
NO_FILE = "فایلی انتخاب نشده است."

ScopeResolver = Callable[[dict], int | None]


def media_view(media: list[dict[str, Any]], base: str, *, reseller_id: int | None) -> list[dict[str, Any]]:
    """What the editor shows for each item; file names stay server-side except in preview URLs."""
    out = []
    for entry in media:
        name = entry.get("file")
        out.append(
            {
                "id": entry["id"],
                "kind": entry["kind"],
                "missing": is_media_missing(entry, reseller_id=reseller_id),
                "preview": f"{base}/file/{name}" if media_path(name, reseller_id=reseller_id) else None,
                "from_bot": bool(entry.get("tg")) and not name,
            }
        )
    return out


async def guides_media_view(session: AsyncSession, base: str, *, reseller_id: int | None) -> dict[str, list]:
    """guide id → editor media rows, for the settings page."""
    guides = await get_connection_guides(session, reseller_id=reseller_id)
    return {
        str(g["id"]): media_view(g.get("media") or [], base, reseller_id=reseller_id) for g in guides if g.get("media")
    }


async def _read_upload(form: Any) -> bytes:
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise GuideMediaError(NO_FILE)
    data = await upload.read(MAX_VIDEO_BYTES + 1)
    if not data:
        raise GuideMediaError(NO_FILE)
    return data


def _error(exc: GuideMediaError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _ok(guide_id: str, media: list[dict[str, Any]], base: str, rid: int | None) -> JSONResponse:
    return JSONResponse({"ok": True, "guide_id": guide_id, "media": media_view(media, base, reseller_id=rid)})


def _register_scope(
    app: FastAPI, base: str, *, dependency: Callable, scope: ScopeResolver, get_db: Callable
) -> None:
    @app.post(f"{base}/upload")
    async def guide_media_upload(
        request: Request, staff: dict = Depends(dependency), session: AsyncSession = Depends(get_db)
    ) -> JSONResponse:
        rid = scope(staff)
        form = await request.form()
        guide_id = str(form.get("guide_id") or "")
        try:
            data = await _read_upload(form)
            kind, extension = check_upload(data)
        except GuideMediaError as exc:
            return _error(exc)
        file_name = await asyncio.to_thread(write_media_file, data, extension, reseller_id=rid)
        try:
            media = await add_guide_media(session, guide_id, kind=kind, file_name=file_name, reseller_id=rid)
        except GuideMediaError as exc:
            await asyncio.to_thread(delete_media_file, file_name, reseller_id=rid)
            return _error(exc)
        return _ok(guide_id, media, base, rid)

    @app.post(f"{base}/delete")
    async def guide_media_delete(
        request: Request, staff: dict = Depends(dependency), session: AsyncSession = Depends(get_db)
    ) -> JSONResponse:
        rid = scope(staff)
        form = await request.form()
        guide_id, media_id = str(form.get("guide_id") or ""), str(form.get("media_id") or "")
        try:
            file_name, media = await remove_guide_media(session, guide_id, media_id, reseller_id=rid)
        except GuideMediaError as exc:
            return _error(exc)
        await asyncio.to_thread(delete_media_file, file_name, reseller_id=rid)
        return _ok(guide_id, media, base, rid)

    @app.post(f"{base}/move")
    async def guide_media_move(
        request: Request, staff: dict = Depends(dependency), session: AsyncSession = Depends(get_db)
    ) -> JSONResponse:
        rid = scope(staff)
        form = await request.form()
        guide_id, media_id = str(form.get("guide_id") or ""), str(form.get("media_id") or "")
        step = -1 if str(form.get("direction") or "") == "up" else 1
        try:
            media = await move_guide_media(session, guide_id, media_id, step=step, reseller_id=rid)
        except GuideMediaError as exc:
            return _error(exc)
        return _ok(guide_id, media, base, rid)

    @app.get(f"{base}/file/{{name}}")
    async def guide_media_file(
        name: str, staff: dict = Depends(dependency), session: AsyncSession = Depends(get_db)
    ) -> FileResponse:
        rid = scope(staff)
        path = media_path(name, reseller_id=rid)
        if path is None or name not in await guide_media_files(session, reseller_id=rid):
            raise HTTPException(status_code=404, detail="فایل پیدا نشد")
        media_type = _MEDIA_TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


def _platform_scope(staff: dict) -> int | None:
    return None


def _shop_scope(staff: dict) -> int | None:
    if is_platform_admin(staff) or staff.get("role") == "admin":
        raise HTTPException(status_code=403, detail="آموزش‌های پلتفرم در «تنظیمات» مدیریت می‌شوند")
    rid = shop_owner_id(staff)
    if not rid:
        raise HTTPException(status_code=403, detail="محدوده فروشگاه مشخص نیست")
    return int(rid)


def register_guide_media_pages(
    app: FastAPI, *, require_admin: Callable, require_shop_settings: Callable, get_db: Callable
) -> None:
    _register_scope(app, PLATFORM_BASE, dependency=require_admin, scope=_platform_scope, get_db=get_db)
    _register_scope(app, SHOP_BASE, dependency=require_shop_settings, scope=_shop_scope, get_db=get_db)

