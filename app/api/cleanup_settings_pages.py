"""Finance cleanup tab: save tag deletion rules, and acknowledge soft-cancel retirement."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.safe_next import safe_internal_next
from app.services.cleanup_settings import (
    ACK_RETIRED_PATH,
    SAVE_PATH,
    ack_soft_cancel_retired,
    save_cleanup_tab,
)

CLEANUP_TAB = "/finance?tab=orders&settings=cleanup"
FINANCE_SETTINGS = "/finance?tab=orders&settings=payment"


def _back(request: Request, *, fallback: str, key: str, value: str) -> RedirectResponse:
    target = safe_internal_next(request.query_params.get("next"), fallback)
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
            return _back(request, fallback=CLEANUP_TAB, key="err", value=str(exc))
        return _back(request, fallback=CLEANUP_TAB, key="ok", value="تنظیمات پاکسازی ذخیره شد")

    @app.post(ACK_RETIRED_PATH)
    async def cleanup_ack_soft_cancel_retired(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        try:
            await ack_soft_cancel_retired(session, staff=staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        return _back(
            request,
            fallback=FINANCE_SETTINGS,
            key="ok",
            value="پیام لغو خودکار قبلی بسته شد",
        )
