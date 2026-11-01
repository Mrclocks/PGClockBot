"""Feature-gated v1 backend adapters; service modules own every domain rule."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.integration_auth import integration_bearer_dependency, register_integration_auth_errors
from app.api.integration_routes import IntegrationRoute
from app.config import get_settings
from app.db.session import SessionLocal
from app.services.integration_authority import IntegrationAuthority
from app.services.integration_capabilities import CapabilitiesView, integration_capabilities
from app.services.integration_catalog import CatalogSelection, PlanPage, list_integration_plans
from app.services.integration_customers import CustomerView, read_integration_customer
from app.services.integration_pagination import IntegrationCursors
from app.services.integration_pg import fetch_pg_evidence
from app.services.integration_rate_limits import RequestBudget
from app.services.integration_service_reads import (
    ReadAccess, ServicePage, ServiceSelection, ServiceView, list_integration_services, read_integration_service,
)


class CustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    external_customer_ref: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    display_name: StrictStr = Field(default="", max_length=128)


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LinkConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: StrictBool


class LinkStarted(BaseModel):
    challenge_id: str
    expires_at: datetime
    start_token: str | None = None


class LinkStatus(BaseModel):
    challenge_id: str
    expires_at: datetime
    state: Literal["cancelled", "linked", "expired", "awaiting_website", "awaiting_telegram"]


class LinkResult(BaseModel):
    linked: bool


def bearer(request: Request) -> str:
    return request.scope["pgclock.integration_bearer"]


def read_access(request: Request, authority: IntegrationAuthority) -> ReadAccess:
    return ReadAccess(bearer(request), authority.pg_evidence)


def _configure_openapi(app: FastAPI) -> None:
    original = app.openapi

    def schema() -> dict:
        document = original()
        document.setdefault("components", {}).setdefault("securitySchemes", {})["IntegrationBearer"] = {
            "type": "http", "scheme": "bearer", "bearerFormat": "PGClockBot integration credential",
        }
        for path, methods in document.get("paths", {}).items():
            if path.startswith("/api/v1/"):
                for method, operation in methods.items():
                    if method in {"get", "post", "delete", "put", "patch"}:
                        operation["security"] = [{"IntegrationBearer": []}]
        return document

    app.openapi = schema


def register_integration_api(app: FastAPI, *, get_db: Callable,
                             sessions: async_sessionmaker[AsyncSession] = SessionLocal,
                             enabled: bool | None = None, pg_verifier: Callable | None = None) -> bool:
    if not (get_settings().integration_api_routes_enabled if enabled is None else enabled):
        return False
    verifier = pg_verifier or partial(fetch_pg_evidence, sessions=sessions)
    router = APIRouter(prefix="/api/v1", route_class=IntegrationRoute, tags=["Integration API v1"])

    def authenticated(*scopes: str, budget: RequestBudget = RequestBudget.READ, read_only: bool = False) -> Callable:
        needs_panel = bool(set(scopes) & {"plans:read", "services:read", "services:provision", "services:renew", "services:addons", "services:access"})
        return integration_bearer_dependency(get_db, required_scopes=frozenset(scopes),
            pg_verifier=verifier if needs_panel else None, budget=budget, read_only=read_only)

    @router.get("/capabilities", response_model=CapabilitiesView)
    async def capabilities(request: Request, authority: IntegrationAuthority = Depends(authenticated("capabilities:read")),
                           session: AsyncSession = Depends(get_db)) -> CapabilitiesView:
        return await integration_capabilities(session, bearer(request), verifier)

    @router.get("/plans", response_model=PlanPage)
    async def plans(request: Request, cursor: str = "", limit: int = 25,
                    authority: IntegrationAuthority = Depends(authenticated("plans:read")),
                    session: AsyncSession = Depends(get_db)) -> PlanPage:
        return await list_integration_plans(session, read_access(request, authority), CatalogSelection(cursor, limit),
                                            IntegrationCursors(get_settings().web_secret))


    @router.get("/customers/{customer_id}", response_model=CustomerView)
    async def customer(request: Request, customer_id: str,
                       authority: IntegrationAuthority = Depends(authenticated("customers:read")),
                       session: AsyncSession = Depends(get_db)) -> CustomerView:
        return await read_integration_customer(session, bearer(request), customer_id)

    @router.get("/services", response_model=ServicePage)
    async def services(request: Request, customer_id: str, cursor: str = "", limit: int = 25,
                       authority: IntegrationAuthority = Depends(authenticated("services:read")),
                       session: AsyncSession = Depends(get_db)) -> ServicePage:
        return await list_integration_services(session, read_access(request, authority), ServiceSelection(customer_id, cursor, limit),
                                               IntegrationCursors(get_settings().web_secret))

    @router.get("/services/{service_id}", response_model=ServiceView)
    async def service(request: Request, service_id: str, customer_id: str,
                      authority: IntegrationAuthority = Depends(authenticated("services:read")),
                      session: AsyncSession = Depends(get_db)) -> ServiceView:
        return await read_integration_service(session, read_access(request, authority), customer_id, service_id)






    register_integration_auth_errors(app)
    app.include_router(router)
    _configure_openapi(app)
    return True
