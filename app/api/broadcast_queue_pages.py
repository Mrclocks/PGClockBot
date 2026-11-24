"""The owner's group-message page (shared routes from broadcast_pages) and stopping a queued owner broadcast."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI

from app.api.broadcast_detail_pages import register_broadcast_detail_routes
from app.api.broadcast_pages import PLATFORM_PATH, BroadcastPage, redirect, register_broadcast_routes
from app.services.broadcast_queue import cancel_broadcast

BROADCAST_PATH = PLATFORM_PATH
PLATFORM_PAGE = BroadcastPage(None, PLATFORM_PATH)


def platform_page(_staff: dict) -> BroadcastPage:
    """The owner always works in the platform tenant; ``require_admin`` has already checked the owner."""
    return PLATFORM_PAGE


def register_platform_broadcast_pages(app: FastAPI, *, render: Callable, require_admin: Callable, get_db: Callable) -> None:
    register_broadcast_routes(
        app, path=PLATFORM_PATH, resolve=platform_page, guard=require_admin, render=render, get_db=get_db
    )
    register_broadcast_detail_routes(
        app, path=PLATFORM_PATH, resolve=platform_page, guard=require_admin, render=render, get_db=get_db
    )


def register_broadcast_queue_pages(app: FastAPI, *, require_admin: Callable, get_db: Callable) -> None:
    @app.post("/broadcast/history/{log_id}/cancel")
    async def broadcast_cancel(log_id: int, staff: dict = Depends(require_admin), session=Depends(get_db)):
        # Platform history only — shop rows are cancelled on /shop-broadcast.
        if await cancel_broadcast(session, log_id, shop_id=None):
            return redirect(PLATFORM_PAGE, ok="ارسال متوقف شد؛ پیام‌های فرستاده‌شده برنمی‌گردند")
        return redirect(PLATFORM_PAGE, err="این ارسال دیگر فعال نیست")
