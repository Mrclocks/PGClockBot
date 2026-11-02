"""Mini App receipt review for platform admins and shop owners.

Thin routes over ``app.services.receipt_review``. The reviewer comes from initData; the
review scope (platform or the owner's shop) is derived from that account, never from
the request.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store
from app.services.db_safe import rollback_quiet
from app.services.miniapp_auth import load_mini_user
from app.services.receipt_review import (
    Reviewer,
    ReviewDenied,
    ReviewError,
    ReviewNotFound,
    approve_receipt,
    mini_reviewer,
    receipt_image,
    reject_receipt,
    review_queue,
)
from app.services.redact import user_safe_error

logger = logging.getLogger(__name__)

_ID_MAX = 2**31 - 1


def _review_http_error(exc: ReviewError) -> HTTPException:
    if isinstance(exc, ReviewDenied):
        return HTTPException(403, str(exc))
    if isinstance(exc, ReviewNotFound):
        return HTTPException(404, str(exc))
    return HTTPException(400, user_safe_error(exc, fallback="انجام نشد"))


def _valid_id(value: int) -> int:
    if not 0 < value <= _ID_MAX:
        raise HTTPException(404, "پرداخت پیدا نشد")
    return value


async def _reviewer(session: AsyncSession, request: Request) -> Reviewer:
    user = await load_mini_user(session, request)
    try:
        return await mini_reviewer(session, user)
    except ReviewError as exc:
        raise _review_http_error(exc) from None


def register_miniapp_review(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/review")
    async def mini_review_queue(request: Request, session: AsyncSession = Depends(get_db)):
        reviewer = await _reviewer(session, request)
        return _no_store(await review_queue(session, reviewer))

    @app.get("/api/mini/review/{payment_id}/receipt")
    async def mini_review_receipt(payment_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        reviewer = await _reviewer(session, request)
        try:
            body, content_type = await receipt_image(session, reviewer, _valid_id(payment_id))
        except ReviewError as exc:
            raise _review_http_error(exc) from None
        return Response(
            content=body,
            media_type=content_type,
            headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/mini/review/{payment_id}/approve")
    async def mini_review_approve(payment_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        reviewer = await _reviewer(session, request)
        try:
            message = await approve_receipt(session, reviewer, _valid_id(payment_id))
        except ReviewError as exc:
            await rollback_quiet(session)
            raise _review_http_error(exc) from None
        return _no_store({"ok": True, "message": message})

    @app.post("/api/mini/review/{payment_id}/reject")
    async def mini_review_reject(payment_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        reviewer = await _reviewer(session, request)
        try:
            await reject_receipt(session, reviewer, _valid_id(payment_id))
        except ReviewError as exc:
            await rollback_quiet(session)
            raise _review_http_error(exc) from None
        return _no_store({"ok": True, "message": "پرداخت رد شد"})
