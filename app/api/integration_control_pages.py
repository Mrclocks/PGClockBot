"""Protected Persian operator forms and a private one-time webhook key result."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
import httpx
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from app.db.session import SessionLocal
from app.services.pasarguard import PasarGuardError
from app.services.csrf import CSRF_COOKIE, csrf_tokens_match
from app.services.integration_controls import ControlChange, control_page, control_signer, control_ticket, execute_control
from app.services.integration_management_forms import bounded_integer
from app.services.integration_policy import IntegrationError
from app.services.integration_reconciliation import reconcile_saved_effect

LABELS = {"queued": "در صف", "running": "در حال اجرا", "outcome_unknown": "نتیجه نامشخص", "needs_review": "نیازمند بررسی",
    "succeeded": "انجام شد", "failed": "ناموفق", "pending": "در انتظار ارسال", "delivering": "در حال ارسال",
    "acknowledged": "دریافت تأیید شد", "exhausted": "تلاش‌ها پایان یافت", "provision": "ایجاد", "renew": "تمدید", "addon": "بسته",
    "intent": "در انتظار بررسی نتیجه", "unknown": "نتیجه نامشخص",
    "operation.updated": "تغییر عملیات", "service.updated": "تغییر سرویس", "customer.telegram_link_changed": "تغییر پیوند تلگرام",
    "packs": "بسته‌های مجاز", "destination": "نشانی وب‌هوک", "signing_key": "کلید امضا", "redeliver": "ارسال دوباره", "reconcile": "بررسی نتیجه"}


async def _read_form(request: Request, action: str) -> tuple[str, ControlChange]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise IntegrationError("invalid_request")
    form = await request.form(max_files=0, max_fields=10)
    if any(len(form.getlist(key)) != 1 for key in form) or not csrf_tokens_match(request.cookies.get(CSRF_COOKIE, ""), form.get("csrf_token", "")):
        raise IntegrationError("invalid_request")
    allowed = {"csrf_token", "ticket", "confirm", "resource_id", "url", "enabled", "pack_ids", "overlap_minutes"}
    if set(form)-allowed or action not in {"packs", "destination", "signing_key", "redeliver", "reconcile"}:
        raise IntegrationError("invalid_request")
    ticket, resource, raw_ids = form.get("ticket", ""), form.get("resource_id", ""), form.get("pack_ids", "")
    if not all(isinstance(value, str) for value in (ticket, resource, raw_ids)) or len(ticket) > 2048 or len(resource) > 40 or len(raw_ids) > 1100 or len(raw_ids.split()) > 100:
        raise IntegrationError("invalid_request")
    change = ControlChange(action, resource, str(form.get("url", "")), form.get("enabled") == "yes",
        frozenset(bounded_integer(value, minimum=1, maximum=2**31-1) for value in raw_ids.split()),
        bounded_integer(form.get("overlap_minutes", "0"), minimum=0, maximum=1440), form.get("confirm") == "yes")
    return ticket, change


def register_control_pages(app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable, get_signer: Callable) -> None:
    async def signer() -> URLSafeTimedSerializer:
        original = await asyncio.to_thread(get_signer)
        return control_signer(original.secret_key)

    @app.get("/integrations/{integration_id}/operations")
    async def page(integration_id: str, request: Request, staff: dict = Depends(require_staff),
                   session: AsyncSession = Depends(get_db)) -> Response:
        try:
            result = await control_page(session, staff, integration_id,
                before_operation=bounded_integer(request.query_params.get("before_operation", "0"), minimum=0, maximum=2**31-1),
                before_event=bounded_integer(request.query_params.get("before_event", "0"), minimum=0, maximum=2**31-1),
                before_native=bounded_integer(request.query_params.get("before_native", "0"), minimum=0, maximum=2**31-1))
            token_signer = await signer()
            tickets = {action: control_ticket(token_signer, result.owner, integration_id, action) for action in ("packs", "destination", "signing_key") if action in result.actions}
            operation_tickets = {row.id: control_ticket(token_signer, result.owner, integration_id, "reconcile", row.id) for row in result.operations
                if "reconcile" in result.actions and row.state in {"outcome_unknown", "needs_review"}}
            event_tickets = {row.id: control_ticket(token_signer, result.owner, integration_id, "redeliver", row.id) for row in result.events
                if "redeliver" in result.actions and row.state in {"pending", "exhausted"}}
            return render(request, "integration_operations.html", {"staff": staff, "page": result, "integration_id": integration_id,
                "tickets": tickets, "operation_tickets": operation_tickets, "event_tickets": event_tickets, "labels": LABELS})
        except IntegrationError as error:
            return render(request, "integration_management_error.html", {"staff": staff, "message": str(error)}, status_code=403)

    @app.post("/integrations/{integration_id}/controls/{action}")
    async def mutate(integration_id: str, action: str, request: Request, staff: dict = Depends(require_staff),
                     session: AsyncSession = Depends(get_db)) -> Response:
        try:
            ticket, change = await _read_form(request, action)
            if action == "signing_key" and request.url.scheme != "https":
                raise IntegrationError("secure_delivery_required")
            result = await execute_control(session, staff, integration_id, change, signer=await signer(), ticket=ticket)
            if result.reconciliation:
                await session.rollback()
                await reconcile_saved_effect(SessionLocal, result.reconciliation)
            if result.issuance:
                return render(request, "integration_webhook_key_once.html", {"staff": staff, "issuance": result.issuance, "integration_id": integration_id})
        except IntegrationError as error:
            await session.rollback()
            return render(request, "integration_management_error.html", {"staff": staff, "message": str(error)}, status_code=409)
        except (SQLAlchemyError, OSError, TimeoutError, httpx.HTTPError, PasarGuardError):
            await session.rollback()
            return render(request, "integration_management_error.html", {"staff": staff, "message": "بررسی در حال حاضر ممکن نیست؛ شواهد عملیات حفظ شده است."}, status_code=503)
        return RedirectResponse(f"/integrations/{integration_id}/operations", status_code=303)
