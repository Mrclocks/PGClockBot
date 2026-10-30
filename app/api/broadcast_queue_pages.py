"""Web route that stops a queued or running owner broadcast."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends
from fastapi.responses import RedirectResponse

from app.services.broadcast_queue import cancel_broadcast

BROADCAST_PATH = "/broadcast"


def _redirect(*, ok: str = "", err: str = "") -> RedirectResponse:
    key, text = ("ok", ok) if ok else ("err", err)
    return RedirectResponse(f"{BROADCAST_PATH}?{key}={quote(text)}", status_code=303)


def register_broadcast_queue_pages(app, *, require_admin, get_db) -> None:
    @app.post("/broadcast/history/{log_id}/cancel")
    async def broadcast_cancel(log_id: int, staff: dict = Depends(require_admin), session=Depends(get_db)):
        if await cancel_broadcast(session, log_id):
            return _redirect(ok="ارسال متوقف شد؛ پیام‌های فرستاده‌شده برنمی‌گردند")
        return _redirect(err="این ارسال دیگر فعال نیست")
