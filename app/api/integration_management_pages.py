"""Protected Persian management pages; bearer values appear only in a private POST result."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Literal

from fastapi import Depends, FastAPI, Request
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from app.api.integration_control_pages import register_control_pages
from app.api.integration_management_security import IntegrationManagementSecurity
from app.services.daily_report import format_jalali_parts
from app.services.db_safe import rollback_quiet
from app.services.integration_admin_audit import AUDIT_PAGE_SIZE, management_audit_page
from app.services.integration_management import execute_management
from app.services.integration_management_forms import (
    ManagementAction, ManagementChange, ManagementTarget, bounded_integer, management_signer, new_management_ticket, parse_management_form,
)
from app.services.integration_management_views import (
    ACTION_LABELS, MANAGEMENT_PAGE_SIZE, ManagementDetail, SCOPE_LABELS, STATUS_LABELS, grant_choices,
    integration_management_detail, integration_management_list,
)
from app.services.integration_policy import ERROR_MESSAGES, IntegrationError

logger = logging.getLogger(__name__)


def _error_response(render: Callable, request: Request, staff: dict, error: IntegrationError) -> Response:
    status = {"not_found": 404, "permission_denied": 403, "principal_inactive": 403, "already_submitted": 409, "secure_delivery_required": 403}.get(error.code, 400)
    return render(request, "integration_management_error.html", {"staff": staff, "message": str(error)}, status_code=status)


async def _submitted_change(request: Request, action: ManagementAction) -> tuple[str, ManagementChange | None]:
    if action in {ManagementAction.ISSUE, ManagementAction.ROTATE} and request.url.scheme != "https":
        raise IntegrationError("secure_delivery_required")
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise IntegrationError("invalid_request")
    form = await request.form(max_files=0, max_fields=12)
    if any(len(form.getlist(key)) != 1 for key in form):
        raise IntegrationError("invalid_request")
    ticket = form.get("ticket")
    if not isinstance(ticket, str):
        raise IntegrationError("invalid_request")
    try:
        change = parse_management_form(form, action)
    except IntegrationError:
        change = None
    return ticket, change


def _detail_tickets(ticket_signer: URLSafeTimedSerializer, page: ManagementDetail, integration_id: str) -> tuple[dict, dict]:
    tickets = {action: new_management_ticket(ticket_signer, page.owner, ManagementTarget(ManagementAction(action), integration_id))
               for action in ("configure", "plans", "issue")}
    credential_tickets = {key.selector: {action: new_management_ticket(ticket_signer, page.owner,
        ManagementTarget(ManagementAction(action), integration_id, key.selector)) for action in ("rotate", "revoke")}
        for key in page.credentials}
    return tickets, credential_tickets


async def _audit_response(request: Request, staff: dict, session: AsyncSession, *, render: Callable,
                          integration_id: str | None = None) -> Response:
    try:
        before = bounded_integer(request.query_params.get("before", "0"), minimum=0, maximum=2**31-1)
        rows = await management_audit_page(session, staff, integration_id, before=before)
    except IntegrationError as exc:
        return _error_response(render, request, staff, exc)
    return render(request, "integration_audit.html", {"staff": staff, "integration_id": integration_id,
        "events": rows, "audit_date": format_jalali_parts, "action_labels": ACTION_LABELS,
        "status_labels": STATUS_LABELS, "scope_labels": SCOPE_LABELS,
        "audit_path": f"/integrations/{integration_id}/audit" if integration_id else "/integrations/audit",
        "next_before": rows[-1].id if len(rows) == AUDIT_PAGE_SIZE else None})


def register_integration_management_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable, get_signer: Callable,
) -> None:
    app.add_middleware(IntegrationManagementSecurity)
    register_control_pages(app, render=render, require_staff=require_staff, get_db=get_db, get_signer=get_signer)

    async def signer() -> URLSafeTimedSerializer:
        session_signer = await asyncio.to_thread(get_signer)
        return management_signer(session_signer.secret_key)

    async def mutate(request: Request, staff: dict, session: AsyncSession, target: ManagementTarget) -> Response:
        try:
            ticket, change = await _submitted_change(request, target.action)
            result = await execute_management(session, staff, target, change, signer=await signer(), ticket=ticket)
        except IntegrationError as exc:
            await rollback_quiet(session)
            return _error_response(render, request, staff, exc)
        except SQLAlchemyError as exc:
            await rollback_quiet(session)
            # Database exception text/parameters can contain a credential digest.
            logger.warning("Integration management storage failure type=%s", type(exc).__name__)
            return render(request, "integration_management_error.html", {
                "staff": staff, "message": ERROR_MESSAGES["authorization_unavailable"],
            }, status_code=503)
        if result.issuance:
            return render(request, "integration_credential_once.html", {"staff": staff, "result": result})
        return RedirectResponse(f"/integrations/{result.integration_id}", status_code=303)

    @app.get("/integrations")
    async def integration_list(request: Request, staff: dict = Depends(require_staff),
                               session: AsyncSession = Depends(get_db)) -> Response:
        try:
            owner, rows = await integration_management_list(session, staff, after=request.query_params.get("after", ""))
            ticket = new_management_ticket(await signer(), owner, ManagementTarget(ManagementAction.CREATE))
        except IntegrationError as exc:
            return _error_response(render, request, staff, exc)
        return render(request, "integrations.html", {"staff": staff, "integrations": rows, "ticket": ticket,
            "status_labels": STATUS_LABELS, "next_after": rows[-1].id if len(rows) == MANAGEMENT_PAGE_SIZE else None})

    @app.get("/integrations/audit")
    async def integration_owner_audit(request: Request, staff: dict = Depends(require_staff),
                                       session: AsyncSession = Depends(get_db)) -> Response:
        return await _audit_response(request, staff, session, render=render)

    @app.get("/integrations/{integration_id}")
    async def integration_detail(integration_id: str, request: Request, staff: dict = Depends(require_staff),
                                 session: AsyncSession = Depends(get_db)) -> Response:
        try:
            plan_after = bounded_integer(request.query_params.get("plan_after", "0"), minimum=0, maximum=2**31-1)
            page = await integration_management_detail(session, staff, integration_id,
                credential_after=request.query_params.get("credential_after", ""), plan_after=plan_after)
            ticket_signer = await signer()
            tickets, credential_tickets = _detail_tickets(ticket_signer, page, integration_id)
        except IntegrationError as exc:
            return _error_response(render, request, staff, exc)
        return render(request, "integration_detail.html", {"staff": staff, "page": page, "tickets": tickets,
            "credential_tickets": credential_tickets, "status_labels": STATUS_LABELS, "scope_labels": SCOPE_LABELS,
            "grant_choices": grant_choices(page.owner), "page_size": MANAGEMENT_PAGE_SIZE})

    @app.get("/integrations/{integration_id}/audit")
    async def integration_audit(integration_id: str, request: Request, staff: dict = Depends(require_staff),
                                session: AsyncSession = Depends(get_db)) -> Response:
        return await _audit_response(request, staff, session, render=render, integration_id=integration_id)

    @app.post("/integrations")
    async def integration_create(request: Request, staff: dict = Depends(require_staff),
                                 session: AsyncSession = Depends(get_db)) -> Response:
        return await mutate(request, staff, session, ManagementTarget(ManagementAction.CREATE))

    @app.post("/integrations/{integration_id}/{action}")
    async def integration_change(integration_id: str, action: Literal["configure", "plans", "issue"], request: Request,
                                 staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)) -> Response:
        return await mutate(request, staff, session, ManagementTarget(ManagementAction(action), integration_id))

    @app.post("/integrations/{integration_id}/credentials/{selector}/{action}")
    async def integration_credential_change(integration_id: str, selector: str, action: Literal["rotate", "revoke"],
                                            request: Request, staff: dict = Depends(require_staff),
                                            session: AsyncSession = Depends(get_db)) -> Response:
        return await mutate(request, staff, session, ManagementTarget(ManagementAction(action), integration_id, selector))
