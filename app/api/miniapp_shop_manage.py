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
from app.services.miniapp_auth import require_shop_miniapp_owner
from app.services.miniapp_shop_manage import (
    BODY_MAX,
    CLIENT_KEY_MAX,
    CLIENT_KEY_MIN,
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
    # Opaque client key so a lost POST + retry does not create a second message.
    client_key: StrictStr = Field(min_length=CLIENT_KEY_MIN, max_length=CLIENT_KEY_MAX)

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("متن پیام را بنویسید")
        return text

    @field_validator("client_key")
    @classmethod
    def strip_client_key(cls, value: str) -> str:
        text = value.strip()
        if not (CLIENT_KEY_MIN <= len(text) <= CLIENT_KEY_MAX):
            raise ValueError("کلید تکرار نامعتبر است")
        if not text.replace("-", "").replace("_", "").isalnum():
            raise ValueError("کلید تکرار نامعتبر است")
        return text


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ManageDenied):
        return HTTPException(403, str(exc))
    if isinstance(exc, ManageNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, user_safe_error(exc, fallback="درخواست نامعتبر است"))
    return HTTPException(400, "انجام نشد")


def register_miniapp_shop_manage(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/shop/customers")
    async def mini_shop_customers(
        request: Request,
        before_id: int | None = Query(None, ge=1, le=ID_MAX),
        limit: int = Query(PAGE_SIZE, ge=1, le=PAGE_SIZE_MAX),
        q: str | None = Query(None, max_length=SEARCH_MAX),
        session: AsyncSession = Depends(get_db),
    ):
        _user, profile, scope = await require_shop_miniapp_owner(session, request)
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
        _user, profile, scope = await require_shop_miniapp_owner(session, request)
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
        _user, profile, scope = await require_shop_miniapp_owner(session, request)
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
        _user, profile, scope = await require_shop_miniapp_owner(session, request)
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
        user, profile, scope = await require_shop_miniapp_owner(session, request)
        try:
            payload = await reply_shop_ticket(
                session,
                shop_id=int(scope.shop_id),
                profile=profile,
                ticket_id=ticket_id,
                staff_telegram_id=int(user.telegram_id or 0),
                staff_name=user.full_name or user.username,
                body=body.body,
                client_key=body.client_key,
            )
        except (ManageDenied, ManageNotFound, ValueError) as exc:
            await rollback_quiet(session)
            raise _http_error(exc) from None
        return _no_store(payload)
