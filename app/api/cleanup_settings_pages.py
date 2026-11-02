"""Finance cleanup tab: one save for the stale-order cancel settings and the deletion rules."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.safe_next import safe_internal_next
from app.services.cleanup_settings import SAVE_PATH, save_cleanup_tab

CLEANUP_TAB = "/finance?tab=orders&settings=cleanup"


def _back(request: Request, key: str, value: str) -> RedirectResponse:
    target = safe_internal_next(request.query_params.get("next"), CLEANUP_TAB)
    joiner = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{joiner}{key}={quote(value)}", status_code=303)


def register_cleanup_settings_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(SAVE_PATH)
    async def cleanup_settings_save(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        form = await request.form()
        try:
            await save_cleanup_tab(session, staff=staff, form=form)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        except ValueError as exc:
            return _back(request, "err", str(exc))
        return _back(request, "ok", "تنظیمات پاکسازی ذخیره شد")
