"""Test send of the message being composed on a group-message page, answered as JSON so the form keeps its text."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broadcast_pages import BroadcastPage
from app.services.broadcast_test_send import send_test_broadcast
from app.services.formatting import format_number
from app.services.message_buttons import broadcast_buttons, form_inputs
from app.services.redact import user_safe_error


def register_broadcast_test_routes(
    app: FastAPI, *, path: str, resolve: Callable[[dict], BroadcastPage], guard: Callable, get_db: Callable
) -> None:
    """``POST {path}/test``: sends the composed message to the sender's own account through the tenant's bot."""

    @app.post(f"{path}/test")
    async def broadcast_test_send(
        request: Request, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)
    ) -> JSONResponse:
        page = resolve(staff)
        form = await request.form()
        try:
            buttons = await broadcast_buttons(session, form_inputs(form), shop_id=page.shop_id)
            delivered = await send_test_broadcast(session, shop_id=page.shop_id, text=form.get("text"), buttons=buttons)
        except ValueError as exc:
            await session.rollback()
            return JSONResponse({"error": user_safe_error(exc)}, status_code=400)
        who = "حساب شما" if delivered == 1 else f"{format_number(delivered)} حساب ادمین"
        return JSONResponse({"message": f"پیام آزمایشی به {who} فرستاده شد؛ در تلگرام ببینید."})
