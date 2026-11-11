"""Shop-owner Mini App management routes (customers / orders / tickets).

Thin over ``app.services.miniapp_shop_manage``. Only the shop Mini App owner reaches
these paths; every list and mutation is scoped to ``MiniScope.shop_id``.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store
from app.services.db_safe import rollback_quiet
from app.services.miniapp_auth import load_mini_user, load_reseller_profile
from app.services.miniapp_shop import SHOP_OWNER_ONLY, is_shop_miniapp_owner, mini_scope
from app.services.miniapp_shop_manage import (
    BODY_MAX,
    ID_MAX,
    PAGE_SIZE,
    PAGE_SIZE_MAX,
    SEARCH_MAX,
    ManageDenied,
    ManageNotFound,
    list_shop_customers,
    list_shop_orders,
    list_shop_tickets,
    reply_shop_ticket,
    shop_ticket_thread,
)
from app.services.redact import user_safe_error

logger = logging.getLogger(__name__)


class TicketReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: StrictStr = Field(min_length=1, max_length=BODY_MAX)

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("متن پیام را بنویسید")
        return text


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ManageDenied):
        return HTTPException(403, str(exc))
    if isinstance(exc, ManageNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, user_safe_error(exc, fallback="درخواست نامعتبر است"))
    return HTTPException(400, "انجام نشد")


async def _shop_owner(session: AsyncSession, request: Request):
    user = await load_mini_user(session, request)
    scope = mini_scope(request)
    if not scope.is_shop or not is_shop_miniapp_owner(scope, user):
        raise HTTPException(403, SHOP_OWNER_ONLY)
    profile = await load_reseller_profile(session, user)
    if profile is None or not profile.is_active or int(profile.user_id) != int(scope.shop_id):
        raise HTTPException(403, SHOP_OWNER_ONLY)
    return user, profile, scope


def register_miniapp_shop_manage(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/shop/customers")
    async def mini_shop_customers(
        request: Request,
        before_id: int | None = Query(None, ge=1, le=ID_MAX),
        limit: int = Query(PAGE_SIZE, ge=1, le=PAGE_SIZE_MAX),
        q: str | None = Query(None, max_length=SEARCH_MAX),
        session: AsyncSession = Depends(get_db),
    ):
        user, profile, scope = await _shop_owner(session, request)
        _ = user
        try:
            payload = await list_shop_customers(
                session,
                shop_id=int(scope.shop_id),
                profile=profile,
                before_id=before_id,
                limit=limit,
                q=q,
            )
        except (ManageDenied, ManageNotFound, ValueError) as exc:
            raise _http_error(exc) from None
        return _no_store(payload)

    @app.get("/api/mini/shop/orders")
    async def mini_shop_orders(
        request: Request,
        before_id: int | None = Query(None, ge=1, le=ID_MAX),
        limit: int = Query(PAGE_SIZE, ge=1, le=PAGE_SIZE_MAX),
        session: AsyncSession = Depends(get_db),
    ):
        user, profile, scope = await _shop_owner(session, request)
        _ = user
        try:
            payload = await list_shop_orders(
                session,
                shop_id=int(scope.shop_id),
                profile=profile,
                before_id=before_id,
                limit=limit,
            )
        except (ManageDenied, ManageNotFound, ValueError) as exc:
            raise _http_error(exc) from None
        return _no_store(payload)

    @app.get("/api/mini/shop/tickets")
    async def mini_shop_tickets(
        request: Request,
        before_id: int | None = Query(None, ge=1, le=ID_MAX),
        limit: int = Query(PAGE_SIZE, ge=1, le=PAGE_SIZE_MAX),
        session: AsyncSession = Depends(get_db),
    ):
        user, profile, scope = await _shop_owner(session, request)
        _ = user
        try:
            payload = await list_shop_tickets(
                session,
                shop_id=int(scope.shop_id),
                profile=profile,
                before_id=before_id,
                limit=limit,
            )
        except (ManageDenied, ManageNotFound, ValueError) as exc:
            raise _http_error(exc) from None
        return _no_store(payload)

    @app.get("/api/mini/shop/tickets/{ticket_id}")
    async def mini_shop_ticket(
        ticket_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        user, profile, scope = await _shop_owner(session, request)
        _ = user
        try:
            payload = await shop_ticket_thread(
                session,
                shop_id=int(scope.shop_id),
                profile=profile,
                ticket_id=ticket_id,
            )
        except (ManageDenied, ManageNotFound, ValueError) as exc:
            raise _http_error(exc) from None
        return _no_store(payload)

    @app.post("/api/mini/shop/tickets/{ticket_id}/reply")
    async def mini_shop_ticket_reply(
        ticket_id: int,
        body: TicketReplyBody,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        user, profile, scope = await _shop_owner(session, request)
        try:
            payload = await reply_shop_ticket(
                session,
                shop_id=int(scope.shop_id),
                profile=profile,
                ticket_id=ticket_id,
                staff_telegram_id=int(user.telegram_id or 0),
                body=body.body,
            )
        except (ManageDenied, ManageNotFound, ValueError) as exc:
            await rollback_quiet(session)
            raise _http_error(exc) from None
        return _no_store(payload)
