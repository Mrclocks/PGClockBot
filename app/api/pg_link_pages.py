"""Owner-only PasarGuard link reconnect route (home health card)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import RedirectResponse

from app.services.authz import authz_from_staff, can_shop, is_explicit_owner_staff
from app.services.pg_link_reconnect import (
    PG_LINK_RECONNECT_PERMISSION,
    flash_for_result,
    reconnect_panel,
)

RECONNECT_PATH = "/pg-link/reconnect"
HOME = "/home"
DENIED_MESSAGE = "دسترسی لازم برای ارتباط مجدد را ندارید"


def _flash_redirect(*, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    if ok:
        return RedirectResponse(f"{HOME}?ok={quote(ok)}", status_code=303)
    return RedirectResponse(f"{HOME}?err={quote(err or '')}", status_code=303)


def _owner_may_reconnect(staff: dict) -> bool:
    if not is_explicit_owner_staff(staff):
        return False
    return can_shop(authz_from_staff(staff), PG_LINK_RECONNECT_PERMISSION)


def register_pg_link_pages(app: FastAPI, *, require_admin: Callable) -> None:
    router = APIRouter()

    @router.post(RECONNECT_PATH)
    async def pg_link_reconnect(staff: dict = Depends(require_admin)) -> RedirectResponse:
        if not _owner_may_reconnect(staff):
            return _flash_redirect(err=DENIED_MESSAGE)
        result = await reconnect_panel()
        key, message = flash_for_result(result)
        if key == "ok":
            return _flash_redirect(ok=message)
        return _flash_redirect(err=message)

    app.include_router(router)
