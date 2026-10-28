"""Thin adapters for durable submissions and explicitly private delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integration_routes import idempotency_key
from app.config import get_settings
from app.services.integration_addons import AddonPage, list_service_addons
from app.services.integration_pagination import IntegrationCursors
from app.services.integration_renewal_preview import RenewalPreview, renewal_preview
from app.services.integration_authority import IntegrationAuthority
from app.services.integration_operation_views import OperationView
from app.services.integration_operations import ExecutionRequest, submit_integration_operation
from app.services.integration_policy import IntegrationError
from app.services.integration_private_access import PrivateAccess, private_service_access, render_access_qr
from app.services.integration_rate_limits import RequestBudget
from app.services.integration_service_reads import ReadAccess


class PlanExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: StrictStr = Field(min_length=1, max_length=40)
    external_operation_ref: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    plan_id: StrictStr = Field(min_length=1, max_length=40)
    terms_revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class AddonExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: StrictStr = Field(min_length=1, max_length=40)
    external_operation_ref: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    pack_id: StrictStr = Field(min_length=1, max_length=40)
    terms_revision: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


def _access(request: Request, authority: IntegrationAuthority) -> ReadAccess:
    return ReadAccess(request.scope["pgclock.integration_bearer"], authority.pg_evidence)


async def _submit(request: Request, authority: IntegrationAuthority, session: AsyncSession,
                  value: PlanExecutionInput, *, kind: str, service_id: str | None = None) -> Response:
    if not get_settings().integration_execution_enabled:
        raise IntegrationError("temporarily_unavailable")
    result = await submit_integration_operation(session, _access(request, authority), ExecutionRequest(
        value.customer_id, value.external_operation_ref, value.plan_id, value.terms_revision, kind, service_id),
        idempotency_key=idempotency_key(request))
    location = f"/api/v1/operations/{result.id}?customer_id={result.customer_id}"
    return JSONResponse(jsonable_encoder(asdict(result)), status_code=202, headers={"Location": location, "Retry-After": "5"})


def register_operation_routes(router: APIRouter, authenticated: Callable, get_db: Callable) -> None:
    @router.post("/provisioning-requests", status_code=202, response_model=OperationView)
    async def provision(request: Request, value: PlanExecutionInput,
                        authority: IntegrationAuthority = Depends(authenticated("services:provision", "operations:read", budget=RequestBudget.WRITE)),
                        session: AsyncSession = Depends(get_db)) -> Response:
        return await _submit(request, authority, session, value, kind="provision")

    @router.post("/services/{service_id}/renewals", status_code=202, response_model=OperationView)
    async def renew(request: Request, service_id: str, value: PlanExecutionInput,
                    authority: IntegrationAuthority = Depends(authenticated("services:renew", "operations:read", budget=RequestBudget.WRITE)),
                    session: AsyncSession = Depends(get_db)) -> Response:
        return await _submit(request, authority, session, value, kind="renew", service_id=service_id)

    @router.get("/services/{service_id}/access", response_model=PrivateAccess)
    async def access(request: Request, service_id: str, customer_id: str,
                     authority: IntegrationAuthority = Depends(authenticated("services:access", "services:read", budget=RequestBudget.PRIVATE)),
                     session: AsyncSession = Depends(get_db)) -> PrivateAccess:
        return await private_service_access(session, _access(request, authority), customer_id, service_id)

    @router.get("/services/{service_id}/qr", response_class=Response)
    async def qr(request: Request, service_id: str, customer_id: str,
                 authority: IntegrationAuthority = Depends(authenticated("services:access", "services:read", budget=RequestBudget.PRIVATE)),
                 session: AsyncSession = Depends(get_db)) -> Response:
        result = await private_service_access(session, _access(request, authority), customer_id, service_id)
        image = await asyncio.to_thread(render_access_qr, result.subscription_url)
        return Response(image, media_type="image/png", headers={"X-Access-Version": str(result.access_version)})

    @router.get("/services/{service_id}/renewal-preview", response_model=RenewalPreview)
    async def preview(request: Request, service_id: str, customer_id: str, plan_id: str,
                      authority: IntegrationAuthority = Depends(authenticated("services:read", "services:renew", "plans:read", read_only=True)),
                      session: AsyncSession = Depends(get_db)) -> RenewalPreview:
        return await renewal_preview(session, _access(request, authority), customer_id, service_id, plan_id)

    @router.get("/services/{service_id}/addons", response_model=AddonPage)
    async def addons(request: Request, service_id: str, customer_id: str, cursor: str = "", limit: int = 25,
                     authority: IntegrationAuthority = Depends(authenticated("services:read", "services:addons", read_only=True)),
                     session: AsyncSession = Depends(get_db)) -> AddonPage:
        return await list_service_addons(session, _access(request, authority), customer_id, service_id,
            IntegrationCursors(get_settings().web_secret), cursor=cursor, limit=limit)

    @router.post("/services/{service_id}/addons", status_code=202, response_model=OperationView)
    async def addon(request: Request, service_id: str, value: AddonExecutionInput,
                    authority: IntegrationAuthority = Depends(authenticated("services:addons", "operations:read", budget=RequestBudget.WRITE)),
                    session: AsyncSession = Depends(get_db)) -> Response:
        plan_value = PlanExecutionInput(customer_id=value.customer_id, external_operation_ref=value.external_operation_ref,
            plan_id=value.pack_id, terms_revision=value.terms_revision)
        return await _submit(request, authority, session, plan_value, kind="addon", service_id=service_id)
