"""Save the phone bottom bar's page order (System settings → «ظاهر و برند»); Owner only."""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.mobile_bar import save_bar_order
from app.services.panel_nav import DOCK_SLOTS

logger = logging.getLogger(__name__)

MOBILE_BAR_PATH = "/settings/mobile-bar"
FORM_PATH = "/settings?tab=pwa"
SLOT_FIELD = "bar_slot"
SAVED = "نوار موبایل ذخیره شد"
RESET = "نوار موبایل به ترتیب پیش‌فرض برگشت"
FAILED = "ذخیرهٔ نوار موبایل ناموفق بود"


def _back(*, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"saved=1&msg={quote(ok)}" if ok else f"err={quote(err)}"
    return RedirectResponse(f"{FORM_PATH}&{query}#mobile-bar", status_code=303)


def register_mobile_bar_settings(app: FastAPI, *, require_admin: Callable, get_db: Callable) -> None:
    @app.post(MOBILE_BAR_PATH)
    async def save_mobile_bar(
        request: Request,
        _staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        form = await request.form()
        reset = form.get("bar_reset") == "1"
        slots = [] if reset else [form.get(f"{SLOT_FIELD}_{i}") for i in range(1, DOCK_SLOTS + 1)]
        try:
            await save_bar_order(session, slots)
        except ValueError as exc:
            await session.rollback()
            return _back(err=str(exc))
        except Exception:
            await session.rollback()
            logger.exception("mobile bar save failed")
            return _back(err=FAILED)
        return _back(ok=RESET if reset else SAVED)
