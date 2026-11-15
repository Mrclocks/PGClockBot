"""Shop staff pages for group messages to that shop's customers only."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.authz import authz_from_staff, can_shop
from app.services.broadcast import clear_broadcast_history, delete_broadcast_log, list_broadcast_history
from app.services.broadcast_queue import STATUS_LABELS, cancel_broadcast
from app.services.shop_broadcast import (
    SHOP_AUDIENCE_LABELS,
    new_shop_request_key,
    queue_shop_broadcast,
)
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id

SHOP_BROADCAST_PATH = "/shop-broadcast"


def shop_broadcast_scope(staff: dict) -> int:
    """Same rule as the marketing tab: ``broadcast`` permission plus a concrete shop id."""
    if not can_shop(authz_from_staff(staff), "broadcast"):
        raise HTTPException(403)
    try:
        scope = resolve_shop_scope_id(staff)
    except ShopScopeError as exc:
        raise HTTPException(403) from exc
    if scope is None:
        raise HTTPException(403)
    return int(scope)


def _redirect(*, ok: str = "", err: str = "") -> RedirectResponse:
    params: list[str] = []
    if ok:
        params.append("ok=" + quote(ok, safe=""))
    if err:
        params.append("err=" + quote(err, safe=""))
    suffix = ("?" + "&".join(params)) if params else ""
    return RedirectResponse(SHOP_BROADCAST_PATH + suffix, status_code=303)


def _actor(staff: dict) -> str:
    return str(
        staff.get("username") or staff.get("bot_user_id") or staff.get("org_principal_id") or "shop"
    )[:128]


def register_shop_broadcast_pages(
    app,
    *,
    render: Callable,
    require_staff: Callable,
    get_db: Callable,
) -> None:
    @app.get(SHOP_BROADCAST_PATH)
    async def shop_broadcast_page(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        scope = shop_broadcast_scope(staff)
        history = await list_broadcast_history(session, limit=40, shop_id=scope)
        return render(
            request,
            "shop_broadcast.html",
            {
                "staff": staff,
                "history": history,
                "audience_labels": SHOP_AUDIENCE_LABELS,
                "status_labels": STATUS_LABELS,
                "request_key": new_shop_request_key(),
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.post(SHOP_BROADCAST_PATH)
    async def shop_broadcast_send(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        scope = shop_broadcast_scope(staff)
        form = await request.form()
        try:
            log = await queue_shop_broadcast(
                session,
                shop_id=scope,
                text=form.get("text"),
                created_by=_actor(staff),
                request_key=form.get("request_key"),
            )
        except ValueError as exc:
            await session.rollback()
            return _redirect(err=str(exc))
        return _redirect(ok=f"در صف ارسال قرار گرفت: {log.total} مخاطب. پیشرفت را در تاریخچه ببینید.")

    @app.post(f"{SHOP_BROADCAST_PATH}/history/clear")
    async def shop_broadcast_history_clear(
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        scope = shop_broadcast_scope(staff)
        n = await clear_broadcast_history(session, shop_id=scope)
        return _redirect(ok=f"{n} رکورد تاریخچه حذف شد")

    @app.post(f"{SHOP_BROADCAST_PATH}/history/{{log_id}}/delete")
    async def shop_broadcast_history_delete(
        log_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        scope = shop_broadcast_scope(staff)
        if not await delete_broadcast_log(session, log_id, shop_id=scope):
            return _redirect(err="رکورد یافت نشد")
        return _redirect(ok="رکورد حذف شد")

    @app.post(f"{SHOP_BROADCAST_PATH}/history/{{log_id}}/cancel")
    async def shop_broadcast_history_cancel(
        log_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        scope = shop_broadcast_scope(staff)
        if await cancel_broadcast(session, log_id, shop_id=scope):
            return _redirect(ok="ارسال متوقف شد؛ پیام‌های فرستاده‌شده برنمی‌گردند")
        return _redirect(err="این ارسال دیگر فعال نیست")
