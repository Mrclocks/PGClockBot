"""Test send of the message being composed on a group-message page, answered as JSON so the form keeps its text."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.api.broadcast_pages import MAX_BODY_BYTES, MEDIA_FIELD, TOO_LARGE, BroadcastPage
from app.services.broadcast_media import MAX_UPLOAD_BYTES
from app.services.broadcast_test_send import TestMedia, checked_test_media, send_test_broadcast
from app.services.formatting import format_number
from app.services.message_buttons import broadcast_buttons, form_inputs
from app.services.redact import user_safe_error
from app.services.security_policy import content_length_ok


async def media_for_test(form: Mapping) -> TestMedia | None:
    """The photo or video chosen in the form, checked and kept in memory; ``None`` when none is chosen."""
    upload = form.get(MEDIA_FIELD)
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    return checked_test_media(data) if data else None


def register_broadcast_test_routes(
    app: FastAPI, *, path: str, resolve: Callable[[dict], BroadcastPage], guard: Callable, get_db: Callable
) -> None:
    """``POST {path}/test``: sends the composed message to the sender's own account through the tenant's bot."""

    @app.post(f"{path}/test")
    async def broadcast_test_send(
        request: Request, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)
    ) -> JSONResponse:
        page = resolve(staff)
        if not content_length_ok(request.headers.get("content-length"), MAX_BODY_BYTES):
            return JSONResponse({"error": TOO_LARGE}, status_code=413)
        form = await request.form()
        try:
            media = await media_for_test(form)
            buttons = await broadcast_buttons(session, form_inputs(form), shop_id=page.shop_id)
            delivered = await send_test_broadcast(
                session, shop_id=page.shop_id, text=form.get("text"), buttons=buttons, media=media
            )
        except ValueError as exc:
            await session.rollback()
            return JSONResponse({"error": user_safe_error(exc)}, status_code=400)
        who = "حساب شما" if delivered == 1 else f"{format_number(delivered)} حساب ادمین"
        return JSONResponse({"message": f"پیام آزمایشی به {who} فرستاده شد؛ در تلگرام ببینید."})
