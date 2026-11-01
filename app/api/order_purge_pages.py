"""Cleanup tab: save which status tags the manual and the hourly run delete, and run it by hand."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.safe_next import safe_internal_next
from app.services.order_purge import (
    RULES_PATH,
    RUN_PATH,
    TAGS,
    PurgeRules,
    count_purgeable,
    parse_days,
    purge_scope,
    record_purge_run,
    rules_from_settings,
    run_purge,
    save_rules,
)
from app.services.users import get_all_settings

CLEANUP_TAB = "/finance?tab=orders&settings=cleanup"


def _back(request: Request, **flash: str) -> RedirectResponse:
    target = safe_internal_next(request.query_params.get("next"), CLEANUP_TAB)
    key, value = next(iter(flash.items()))
    joiner = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{joiner}{key}={quote(value)}", status_code=303)


def _switched_on(form, prefix: str) -> frozenset[str]:
    return frozenset(token for token in TAGS if str(form.get(f"{prefix}{token}") or "") == "1")


def register_order_purge_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(RULES_PATH)
    async def purge_rules_save(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        try:
            shop_id = purge_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            days = parse_days(form.get("days"))
        except ValueError as exc:
            return _back(request, err=str(exc))
        rules = PurgeRules(manual=_switched_on(form, "m_"), auto=_switched_on(form, "a_"), days=days)
        await save_rules(session, shop_id=shop_id, rules=rules)
        return _back(request, ok="قواعد حذف ذخیره شد")

    @app.post(RUN_PATH)
    async def purge_run_now(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        try:
            shop_id = purge_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        rules = rules_from_settings(await get_all_settings(session, reseller_id=shop_id))
        if not rules.manual:
            return _back(request, err="برای حذف دستی هیچ وضعیتی روشن نیست")
        done = await run_purge(session, shop_id=shop_id, tags=rules.manual, days=rules.days)
        await record_purge_run(session, shop_id=shop_id, kind="manual", counts=done)
        left = await count_purgeable(session, shop_id=shop_id, tags=rules.manual, days=rules.days)
        message = f"{done.orders} سفارش و {done.payments} پرداخت کامل حذف شد"
        if left.total:
            message += f" · {left.total} ردیف دیگر مانده؛ دوباره اجرا کنید"
        return _back(request, ok=message)
