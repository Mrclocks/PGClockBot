"""Saved button sets for broadcasts and direct messages: create or replace, and delete.

The platform owner manages the platform's sets from «پیام گروهی»; a shop's staff with the
``broadcast`` permission manage their shop's sets from the shop's broadcast page. The shop is always
the one resolved from the session, never a form field.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.shop_broadcast_pages import SHOP_BROADCAST_PATH, shop_broadcast_scope
from app.services.message_buttons import ButtonRules, delete_preset, save_preset, shop_may_link
from app.services.shop_scope import is_platform_admin

PRESETS_PATH = "/message-buttons/presets"
PLATFORM_BROADCAST_PATH = "/broadcast"


def presets_scope(staff: dict) -> int | None:
    """None for the platform owner, else the staff member's shop (``broadcast`` permission required)."""
    return None if is_platform_admin(staff) else shop_broadcast_scope(staff)


def _back(shop_id: int | None, *, ok: str = "", err: str = "") -> RedirectResponse:
    page = PLATFORM_BROADCAST_PATH if shop_id is None else SHOP_BROADCAST_PATH
    query = f"?ok={quote(ok, safe='')}" if ok else (f"?err={quote(err, safe='')}" if err else "")
    return RedirectResponse(page + query, status_code=303)


def register_message_button_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(PRESETS_PATH)
    async def save_button_preset(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        shop_id = presets_scope(staff)
        form = await request.form()
        try:
            await save_preset(
                session,
                shop_id,
                name=str(form.get("name") or ""),
                buttons=str(form.get("buttons") or ""),
                rules=ButtonRules(allow_links=await shop_may_link(session, shop_id)),
            )
        except ValueError as exc:
            return _back(shop_id, err=str(exc))
        return _back(shop_id, ok="قالب دکمه ذخیره شد")

    @app.post(PRESETS_PATH + "/{preset_id}/delete")
    async def delete_button_preset(
        preset_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        shop_id = presets_scope(staff)
        if not await delete_preset(session, shop_id, preset_id):
            return _back(shop_id, err="قالب پیدا نشد")
        return _back(shop_id, ok="قالب حذف شد")
