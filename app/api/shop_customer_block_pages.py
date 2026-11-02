"""Shop staff block or unblock a customer in their own shop (the customer edit modal)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.user_pages import _redirect_user
from app.services.shop_customer_block import block_shop, set_shop_block
from app.services.shop_manual_adjust import ManualAdjustError

SHOP_BLOCK_PATH = "/users/{user_id}/shop-block"
ACTIONS = {"block": True, "unblock": False}


def register_shop_customer_block_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(SHOP_BLOCK_PATH)
    async def shop_customer_block(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            block_shop(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        action = str(form.get("action") or "")
        if action not in ACTIONS:
            return _redirect_user(user_id, err="عملیات نامعتبر")
        try:
            changed = await set_shop_block(
                session, staff=staff, customer_id=user_id, blocked=ACTIONS[action], reason=form.get("reason")
            )
        except ManualAdjustError as exc:
            return _redirect_user(user_id, err=str(exc))
        if not changed:
            return _redirect_user(user_id, ok="وضعیت مشتری از قبل همین بود")
        done = "مشتری در این فروشگاه مسدود شد" if ACTIONS[action] else "مسدودی مشتری در این فروشگاه برداشته شد"
        return _redirect_user(user_id, ok=done)
