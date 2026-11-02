"""Panel forms on the «مینی‌اپ» settings tab: brand colour, notice and the reseller switch."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.miniapp_branding import ACCENT_SETTING, NOTICE_SETTING, save_mini_brand
from app.services.miniapp_shop import PLATFORM_SWITCH, set_platform_shop_miniapps, switch_value
from app.services.reseller_bots import schedule_shop_menu_sync
from app.services.shop_scope import is_platform_admin, shop_owner_id

PLATFORM_BACK = "/settings?tab=miniapp"
SHOP_BACK = "/shop-settings?tab=miniapp"
SAVED_MESSAGE = "تنظیمات مینی‌اپ ذخیره شد"
NO_SHOP = "فروشگاه شما پیدا نشد"


def _form_values(form) -> tuple[str, str]:
    """The submitted accent (a preset, or "" for the default) and notice."""
    return str(form.get(ACCENT_SETTING) or ""), str(form.get(NOTICE_SETTING) or "")


async def _save(session: AsyncSession, form, *, shop_id: int | None, back: str) -> RedirectResponse | None:
    """None when saved; a redirect carrying the error otherwise."""
    accent, notice = _form_values(form)
    try:
        await save_mini_brand(session, shop_id=shop_id, accent=accent, notice=notice)
    except ValueError as exc:
        return RedirectResponse(f"{back}&err={quote(str(exc))}", status_code=303)
    return None


def _saved(back: str) -> RedirectResponse:
    return RedirectResponse(f"{back}&saved=1&msg={quote(SAVED_MESSAGE)}", status_code=303)


def register_miniapp_brand_pages(app: FastAPI, *, require_admin, require_shop_settings, get_db) -> None:
    @app.post("/settings/miniapp-brand")
    async def platform_miniapp_brand_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        form = await request.form()
        failed = await _save(session, form, shop_id=None, back=PLATFORM_BACK)
        if failed is not None:
            return failed
        if await set_platform_shop_miniapps(session, enabled=switch_value(form, PLATFORM_SWITCH) == "1"):
            # Shop bots' «Open» buttons appear or go away with the switch.
            schedule_shop_menu_sync()
        return _saved(PLATFORM_BACK)

    @app.post("/shop-settings/miniapp-brand")
    async def shop_miniapp_brand_save(
        request: Request,
        staff: dict = Depends(require_shop_settings),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        """The shop always comes from the signed-in staff, never from the form."""
        if is_platform_admin(staff) or staff.get("role") == "admin":
            return RedirectResponse(PLATFORM_BACK, status_code=303)
        shop_id = shop_owner_id(staff)
        if not shop_id:
            return RedirectResponse(f"{SHOP_BACK}&err={quote(NO_SHOP)}", status_code=303)
        failed = await _save(session, await request.form(), shop_id=int(shop_id), back=SHOP_BACK)
        return failed or _saved(SHOP_BACK)
