"""JSON endpoints behind the «افزودن سرویس قبلی» settings tab: status, link test, recent tries."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.payer_notify import notify_payer
from app.services.service_import import ServiceImportError
from app.services.service_import import MAX_LINK_LENGTH
from app.services.service_import_requests import Decision, approve_request, list_requests, reject_request
from app.services.service_import_admin import import_scope, recent_attempts, status_snapshot, test_link

STATUS_PATH = "/service-import/status"
TEST_PATH = "/service-import/test"
REQUESTS_PATH = "/service-import/requests"
APPROVE_PERM = "service_import"
NOTICE_TITLE = "➕ افزودن سرویس قبلی"
APPROVED_NOTICE = "✅ درخواست شما تأیید شد و سرویس به «سرویس‌های من» اضافه شد."
REJECTED_NOTICE = "درخواست افزودن سرویس شما تأیید نشد. برای اطلاعات بیشتر با پشتیبانی تماس بگیرید."
NO_ACCESS = {"ok": False, "error": "دسترسی ندارید"}


def register_service_import_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.get(STATUS_PATH)
    async def service_import_status(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        try:
            scope = import_scope(staff)
        except PermissionError:
            return JSONResponse(NO_ACCESS, status_code=403)
        refresh = request.query_params.get("refresh") == "1"
        snapshot = await status_snapshot(session, scope, refresh=refresh)
        snapshot["attempts"] = await recent_attempts(session, scope)
        return JSONResponse(snapshot)

    @app.post(TEST_PATH)
    async def service_import_test(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        try:
            scope = import_scope(staff)
        except PermissionError:
            return JSONResponse(NO_ACCESS, status_code=403)
        form = await request.form()
        link = str(form.get("link") or "")[:MAX_LINK_LENGTH]
        return JSONResponse(await test_link(session, scope, link))

    @app.get(REQUESTS_PATH)
    async def service_import_requests(
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        try:
            scope = import_scope(staff, APPROVE_PERM)
        except PermissionError:
            return JSONResponse(NO_ACCESS, status_code=403)
        return JSONResponse({"ok": True, "requests": await list_requests(session, scope)})

    @app.post(REQUESTS_PATH + "/{request_id}/{action}")
    async def service_import_decide(
        request_id: int,
        action: str,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        try:
            scope = import_scope(staff, APPROVE_PERM)
        except PermissionError:
            return JSONResponse(NO_ACCESS, status_code=403)
        if action not in {"approve", "reject"}:
            return JSONResponse({"ok": False, "error": "عملیات نامعتبر است"}, status_code=404)
        decide = approve_request if action == "approve" else reject_request
        try:
            decision = await decide(session, request_id, scope, decided_by=_decider(staff))
        except (ValueError, ServiceImportError) as exc:
            # ServiceImportError texts are written for customers; ValueError is the "already decided" notice.
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        await _tell_customer(session, decision)
        return JSONResponse({"ok": True, "approved": decision.approved})


def _decider(staff: dict) -> str:
    who = staff.get("username") or staff.get("bot_user_id") or ""
    return f"{staff.get('role') or 'staff'}:{who}"


async def _tell_customer(session: AsyncSession, decision: Decision) -> None:
    await notify_payer(
        session, decision.user_id, title=NOTICE_TITLE,
        body=APPROVED_NOTICE if decision.approved else REJECTED_NOTICE,
        shop_id=decision.shop_id,
    )
