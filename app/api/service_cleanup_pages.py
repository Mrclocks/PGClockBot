"""The expired-service cleanup page: the shop's (or the platform's) rule and a read-only preview."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_reasons import ReasonRequired, reason_from_form
from app.services.service_cleanup_manage import AuditEntry, extend_deadline, list_exempt, parse_hold_days, set_exempt
from app.services.shop_scope import resolve_shop_scope_id
from app.services.staff_audit import actor_from_staff
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
        exempt = await list_exempt(session, shop_id=shop_id)
        return render(
            request,
            "service_cleanup.html",
            {
                "staff": staff,
                "policy": policy,
                "preview": preview,
                "exempt": exempt,
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

    @app.post(SERVICE_CLEANUP_PATH + "/{service_id}/exempt")
    async def service_cleanup_exempt(
        service_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> Response:
        try:
            shop_id = cleanup_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            reason = reason_from_form(form)
        except ReasonRequired as exc:
            return _back(err=str(exc))
        keep = str(form.get("value") or "") == "1"
        audit = AuditEntry(
            actor_from_staff(staff, shop_id=resolve_shop_scope_id(staff)),
            "service_cleanup_exempt" if keep else "service_cleanup_unexempt",
            reason,
        )
        try:
            await set_exempt(session, shop_id=shop_id, service_id=service_id, exempt=keep, audit=audit)
        except ValueError as exc:
            return _back(err=str(exc))
        return _back(ok="سرویس از پاکسازی معاف شد" if keep else "سرویس دوباره در پاکسازی قرار گرفت")

    @app.post(SERVICE_CLEANUP_PATH + "/{service_id}/hold")
    async def service_cleanup_hold(
        service_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> Response:
        try:
            shop_id = cleanup_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            reason = reason_from_form(form)
            days = parse_hold_days(form.get("days"))
        except (ReasonRequired, CleanupPolicyError) as exc:
            return _back(err=str(exc))
        policy = await load_policy(session, shop_id=shop_id)
        audit = AuditEntry(actor_from_staff(staff, shop_id=resolve_shop_scope_id(staff)), "service_cleanup_hold", reason)
        try:
            until = await extend_deadline(
                session, shop_id=shop_id, service_id=service_id, days=days, policy=policy, audit=audit
            )
        except ValueError as exc:
            return _back(err=str(exc))
        return _back(ok=f"مهلت حذف تا {until} تمدید شد")
