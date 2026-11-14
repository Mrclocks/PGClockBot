"""Cleanup tab: run the saved deletion rules by hand (the rules are saved with the tab)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.safe_next import safe_internal_next
from app.services.order_purge import (
    RUN_PATH,
    count_purgeable,
    purge_scope,
    record_purge_run,
    rules_from_settings,
    run_purge,
)
from app.services.users import get_all_settings

PURGE_TAB = "/finance?tab=orders&settings=cleanup"


def _back(request: Request, **flash: str) -> RedirectResponse:
    target = safe_internal_next(request.query_params.get("next"), PURGE_TAB)
    key, value = next(iter(flash.items()))
    joiner = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{joiner}{key}={quote(value)}", status_code=303)


def register_order_purge_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(RUN_PATH)
    async def purge_run_now(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        try:
            shop_id = purge_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        rules = rules_from_settings(await get_all_settings(session, reseller_id=shop_id))
        if not rules.tags:
            return _back(request, err="هیچ وضعیتی برای حذف روشن نیست")
        done = await run_purge(session, shop_id=shop_id, tags=rules.tags, days=rules.days)
        await record_purge_run(session, shop_id=shop_id, kind="manual", counts=done)
        left = await count_purgeable(session, shop_id=shop_id, tags=rules.tags, days=rules.days)
        message = f"{done.orders} سفارش و {done.payments} پرداخت کامل حذف شد"
        if left.total:
            message += f" · {left.total} ردیف دیگر مانده؛ دوباره اجرا کنید"
        return _back(request, ok=message)
