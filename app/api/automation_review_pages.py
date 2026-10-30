"""Web route that releases an automatic purchase parked for review (shop-scoped)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.services.authz import authz_from_staff, can_shop
from app.services.automation_review import clear_review
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id

PERMISSION = "orders"
AUTOMATION_PATH = "/finance?tab=automation"
FLASH_LIMIT = 180


def review_scope(staff: dict) -> int | None:
    """Shop the staff member may release purchases for; 403 without the permission or a shop scope."""
    if not can_shop(authz_from_staff(staff), PERMISSION):
        raise HTTPException(403)
    try:
        return resolve_shop_scope_id(staff)
    except ShopScopeError:
        raise HTTPException(403) from None


def _redirect(error: str = "") -> RedirectResponse:
    text = " ".join(error.split())[:FLASH_LIMIT]
    return RedirectResponse(AUTOMATION_PATH + (f"&error={quote(text)}" if text else ""), status_code=303)


def register_automation_review_pages(app, *, require_staff, get_db) -> None:
    @app.post("/service-automation/{service_id}/clear")
    async def automation_clear(
        request: Request, service_id: int, staff: dict = Depends(require_staff), session=Depends(get_db)
    ):
        scope = review_scope(staff)
        form = await request.form()
        actor = str(staff.get("org_principal_id") or staff.get("username") or staff.get("bot_user_id") or "operator")[:128]
        try:
            await clear_review(
                session,
                service_id,
                shop_id=scope,
                actor=actor,
                money_settled=str(form.get("money_settled", "")) == "1",
                disable_automation=str(form.get("disable_automation", "")) == "1",
            )
        except ValueError as exc:
            await session.rollback()
            return _redirect(str(exc))
        return _redirect()
