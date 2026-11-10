"""The expired-service cleanup page: the shop's (or the platform's) rule and a read-only preview."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.service_cleanup import (
    MAX_DAYS,
    MIN_DAYS,
    CleanupPolicyError,
    build_preview,
    cleanup_scope,
    load_policy,
    parse_policy_form,
    save_policy,
)

SERVICE_CLEANUP_PATH = "/service-cleanup"
SAVED_TEXT = "تنظیمات پاکسازی سرویس‌ها ذخیره شد"


def _back(**flash: str) -> RedirectResponse:
    key, value = next(iter(flash.items()))
    return RedirectResponse(f"{SERVICE_CLEANUP_PATH}?{key}={quote(value)}", status_code=303)


def register_service_cleanup_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable
) -> None:
    @app.get(SERVICE_CLEANUP_PATH)
    async def service_cleanup_page(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> Response:
        try:
            shop_id = cleanup_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        policy = await load_policy(session, shop_id=shop_id)
        preview = await build_preview(session, shop_id=shop_id, policy=policy)
        return render(
            request,
            "service_cleanup.html",
            {
                "staff": staff,
                "policy": policy,
                "preview": preview,
                "min_days": MIN_DAYS,
                "max_days": MAX_DAYS,
                "save_action": SERVICE_CLEANUP_PATH,
            },
        )

    @app.post(SERVICE_CLEANUP_PATH)
    async def service_cleanup_save(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> Response:
        try:
            shop_id = cleanup_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            policy = parse_policy_form(form)
        except CleanupPolicyError as exc:
            return _back(err=str(exc))
        await save_policy(session, shop_id=shop_id, policy=policy)
        return _back(ok=SAVED_TEXT)
