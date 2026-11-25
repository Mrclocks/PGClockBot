"""Shop staff pages for group messages to that shop's customers only.

The page, audience count, send and history routes are the shared group-message routes (broadcast_pages) bound
to the shop resolved from the session; only stopping a running message is specific to this path.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broadcast_detail_pages import register_broadcast_detail_routes
from app.api.broadcast_pages import SHOP_PATH, BroadcastPage, redirect, register_broadcast_routes
from app.services.authz import authz_from_staff, can_shop
from app.services.broadcast_queue import cancel_broadcast
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id

SHOP_BROADCAST_PATH = SHOP_PATH


def shop_broadcast_scope(staff: dict) -> int:
    """Same rule as the sidebar entry: ``broadcast`` permission plus a concrete shop id."""
    if not can_shop(authz_from_staff(staff), "broadcast"):
        raise HTTPException(403)
    try:
        scope = resolve_shop_scope_id(staff)
    except ShopScopeError as exc:
        raise HTTPException(403) from exc
    if scope is None:
        raise HTTPException(403)
    return int(scope)


def shop_page(staff: dict) -> BroadcastPage:
    return BroadcastPage(shop_broadcast_scope(staff), SHOP_PATH)


def register_shop_broadcast_pages(
    app,
    *,
    render: Callable,
    require_staff: Callable,
    get_db: Callable,
) -> None:
    register_broadcast_routes(app, path=SHOP_PATH, resolve=shop_page, guard=require_staff, render=render, get_db=get_db)
    register_broadcast_detail_routes(app, path=SHOP_PATH, resolve=shop_page, guard=require_staff, render=render, get_db=get_db)

    @app.post(f"{SHOP_PATH}/history/{{log_id}}/cancel")
    async def shop_broadcast_history_cancel(
        log_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        page = shop_page(staff)
        if await cancel_broadcast(session, log_id, shop_id=page.shop_id):
            return redirect(page, ok="ارسال متوقف شد؛ پیام‌های فرستاده‌شده برنمی‌گردند")
        return redirect(page, err="این ارسال دیگر فعال نیست")
