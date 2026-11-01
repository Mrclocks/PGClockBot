"""Save the web panel's brand (name, logo, colour) for the signed-in person's own scope."""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.services.authz import authz_from_staff, can_shop
from app.services.panel_brand import LOGO_MAX_BYTES, BrandError, editable_scope, save_brand

logger = logging.getLogger(__name__)

PANEL_BRAND_PATH = "/panel-brand"
OWNER_FORM_PATH = "/settings?tab=pwa"
SHOP_FORM_PATH = "/shop-settings?tab=brand"
SHOP_SETTINGS_PERMISSION = "shop_settings"
# One file plus the short text fields; anything larger is refused before the body is parsed.
MAX_BODY_BYTES = LOGO_MAX_BYTES + 64 * 1024
TOO_LARGE = "حجم فایل ارسالی بیش از حد مجاز است"
SAVED = "برند پنل ذخیره شد"
FAILED = "ذخیرهٔ برند ناموفق بود"


def _back(path: str, *, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"saved=1&msg={quote(ok)}" if ok else f"err={quote(err)}"
    return RedirectResponse(f"{path}&{query}", status_code=303)


def _body_too_large(request: Request) -> bool:
    raw = request.headers.get("content-length", "")
    return raw.isascii() and raw.isdigit() and int(raw) > MAX_BODY_BYTES


def register_panel_brand_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(PANEL_BRAND_PATH)
    async def save_panel_brand(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        # The scope comes from who is signed in, never from the form.
        allowed, shop_id = editable_scope(
            staff, shop_settings_allowed=can_shop(authz_from_staff(staff), SHOP_SETTINGS_PERMISSION)
        )
        if not allowed:
            raise HTTPException(403)
        back = OWNER_FORM_PATH if shop_id is None and staff.get("role") == "admin" else SHOP_FORM_PATH
        if _body_too_large(request):
            return _back(back, err=TOO_LARGE)
        form = await request.form()
        logo = form.get("panel_brand_logo")
        logo_bytes: bytes | None = None
        if isinstance(logo, UploadFile) and logo.filename:
            logo_bytes = await logo.read(LOGO_MAX_BYTES + 1)
            if len(logo_bytes) > LOGO_MAX_BYTES:
                return _back(back, err=TOO_LARGE)
        try:
            await save_brand(
                session,
                shop_id=shop_id,
                name=form.get("panel_brand_name"),
                color=form.get("panel_brand_color"),
                logo_bytes=logo_bytes,
                clear_logo=bool(form.get("panel_brand_logo_clear")),
            )
        except BrandError as exc:
            await session.rollback()
            return _back(back, err=str(exc))
        except Exception:
            await session.rollback()
            logger.exception("panel brand save failed")
            return _back(back, err=FAILED)
        return _back(back, ok=SAVED)
