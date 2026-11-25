"""Turn a customer or a shop off with all its services, or back on (customer edit modal, reseller list)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.user_pages import _redirect_user
from app.services.account_hold_actions import disable_customer, disable_shop, enable_customer, enable_shop
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)

CUSTOMER_HOLD_PATH = "/users/{user_id}/account-hold"
SHOP_HOLD_PATH = "/resellers/{user_id}/account-hold"
FAILED = "تغییر وضعیت حساب انجام نشد؛ دوباره تلاش کنید"
DONE = {
    ("customer", "disable"): "حساب غیرفعال شد؛ سرویس‌ها در حال خاموش شدن هستند",
    ("customer", "enable"): "حساب در حال فعال شدن است؛ سرویس‌هایی که خاموش شده بودند روشن می‌شوند",
    ("shop", "disable"): "نماینده غیرفعال شد؛ ادمین و کاربرانش در حال خاموش شدن هستند",
    ("shop", "enable"): "نماینده در حال فعال شدن است؛ آنچه خاموش شده بود روشن می‌شود",
}


def _redirect_shop(*, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    key, text = ("err", err) if err else ("ok", ok or "")
    return RedirectResponse(f"/resellers?{key}={quote(text)}", status_code=303)


async def _act(session: AsyncSession, call: Callable[[], Awaitable[None]], kind: str, action: str,
               redirect: Callable[..., RedirectResponse]) -> RedirectResponse:
    try:
        await call()
    except (PermissionError, ValueError) as exc:
        await rollback_quiet(session)
        return redirect(err=str(exc))
    except Exception:
        logger.exception("account hold %s %s failed", kind, action)
        await rollback_quiet(session)
        return redirect(err=FAILED)
    return redirect(ok=DONE[(kind, action)])


def register_account_hold_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(CUSTOMER_HOLD_PATH)
    async def customer_account_hold(
        user_id: int, request: Request, staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        form = await request.form()
        action = str(form.get("action") or "")

        def redirect(**kw: str) -> RedirectResponse:
            return _redirect_user(user_id, **kw)

        if action == "disable":
            return await _act(session, lambda: disable_customer(session, staff, user_id, form.get("reason")),
                              "customer", action, redirect)
        if action == "enable":
            return await _act(session, lambda: enable_customer(session, staff, user_id), "customer", action, redirect)
        return redirect(err="عملیات نامعتبر")

    @app.post(SHOP_HOLD_PATH)
    async def shop_account_hold(
        user_id: int, request: Request, staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        form = await request.form()
        action = str(form.get("action") or "")
        if action == "disable":
            return await _act(session, lambda: disable_shop(session, staff, user_id, form.get("reason")),
                              "shop", action, _redirect_shop)
        if action == "enable":
            return await _act(session, lambda: enable_shop(session, staff, user_id), "shop", action, _redirect_shop)
        return _redirect_shop(err="عملیات نامعتبر")
