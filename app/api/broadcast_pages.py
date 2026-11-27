"""Group-message pages: one page and one set of routes for the platform owner and for a shop.

The platform page (``/broadcast``) is the owner's; a shop page (``/shop-broadcast``) is a shop's staff with the
``broadcast`` permission, scoped to the shop resolved from the session, never from a form field. Both compose a
message with an audience (segment and filters), buttons and a live preview, show how many people it reaches,
and list the tenant's history. Sending queues the message (broadcast_queue); a background worker delivers it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.db.models import BroadcastLog
from app.services.broadcast import clear_broadcast_history, delete_broadcast_log, list_broadcast_history
from app.services.broadcast_audience import (
    DAYS_MAX,
    DAYS_MIN,
    GROUPS,
    checked_spec,
    count_audience,
    plan_choices,
    segments_for,
    spec_from_form,
    stored_label,
    takes_filters,
)
from app.services.broadcast_media import CAPTION_MAX, MAX_UPLOAD_BYTES, BroadcastMedia, delete_upload, media_label, write_upload
from app.services.broadcast_queue import (
    STATUS_LABELS,
    STATUS_QUEUED,
    BroadcastRequest,
    new_request_token,
    queue_broadcast,
)
from app.services.broadcast_schedule import day_choices, schedule_label, send_time_from_form
from app.services.campaigns import RECIPIENT_LIMIT
from app.services.marketing_nav import can_open_campaigns
from app.services.message_buttons import broadcast_buttons, buttons_form_context, form_inputs
from app.services.redact import user_safe_error
from app.services.security_policy import content_length_ok
from app.services.shop_broadcast import queue_shop_broadcast

PLATFORM_PATH = "/broadcast"
SHOP_PATH = "/shop-broadcast"
CAMPAIGNS_PATH = "/campaigns"
HISTORY_LIMIT = 40
TEMPLATE = "broadcast.html"
MEDIA_FIELD = "media"
# Room for the text, buttons and filters next to the largest media file.
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 256 * 1024
TOO_LARGE = "حجم فایل بیش از حد مجاز است"
# Drawing of each segment's card; the labels and hints come from broadcast_audience.
SEGMENT_ICONS: dict[str, str] = {
    "all": "users",
    "users": "user",
    "active": "check-circle",
    "expiring": "time",
    "expired": "refresh",
    "no_service": "minus",
    "buyers": "cart",
    "loyal": "loyalty",
    "never_bought": "plus-square",
    "trial": "trial",
    "inactive": "pulse",
    "new": "plus",
    "resellers": "reseller",
    "admins": "admins",
}


@dataclass(frozen=True, slots=True)
class BroadcastPage:
    """Which tenant a request works in: the platform (``shop_id`` None) or one shop, and the page path."""

    shop_id: int | None
    path: str

    @property
    def is_platform(self) -> bool:
        return self.shop_id is None


def redirect(page: BroadcastPage, *, ok: str = "", err: str = "") -> RedirectResponse:
    key, text = ("ok", ok) if ok else ("err", err)
    query = f"?{key}={quote(text, safe='')}" if text else ""
    return RedirectResponse(page.path + query, status_code=303)


def _segment_groups(page: BroadcastPage) -> list[dict[str, object]]:
    """Segment cards grouped for the picker; a group with no segment for this tenant is left out."""
    segments = segments_for(page.shop_id)
    groups = []
    for key, label in GROUPS:
        cards = [
            {"key": s.key, "label": s.label, "hint": s.hint, "icon": SEGMENT_ICONS.get(s.key, "users"), "days": s.days}
            for s in segments
            if s.group == key
        ]
        if cards:
            groups.append({"key": key, "label": label, "segments": cards})
    return groups


def queued_message(log: BroadcastLog) -> str:
    """The confirmation after a submit: when it goes out and how many people it reaches."""
    if log.send_after is not None:
        return f"زمان‌بندی شد برای {schedule_label(log.send_after)}: حدود {log.total} مخاطب. پیشرفت را در تاریخچه ببینید."
    return f"در صف ارسال قرار گرفت: {log.total} مخاطب. پیشرفت را در تاریخچه ببینید."


def schedule_labels(rows: list[BroadcastLog]) -> dict[int, str]:
    """When each broadcast that has not started yet is scheduled to go out."""
    return {
        int(row.id): schedule_label(row.send_after)
        for row in rows
        if row.send_after is not None and row.status == STATUS_QUEUED
    }


def history_labels(rows: list[BroadcastLog]) -> dict[int, str]:
    """The audience summary of each history row."""
    return {int(row.id): stored_label(row.audience_spec, row.audience, shop_id=row.shop_id) for row in rows}


async def page_context(session: AsyncSession, page: BroadcastPage) -> dict[str, object]:
    history = await list_broadcast_history(session, limit=HISTORY_LIMIT, shop_id=page.shop_id)
    segments = segments_for(page.shop_id)
    return {
        **(await buttons_form_context(session, page.shop_id)),
        "page_path": page.path,
        "is_platform": page.is_platform,
        "segment_groups": _segment_groups(page),
        "days_segments": ",".join(s.key for s in segments if s.days),
        "filter_segments": ",".join(s.key for s in segments if takes_filters(s)),
        "days_min": DAYS_MIN,
        "days_max": DAYS_MAX,
        "audience_plans": await plan_choices(session, shop_id=page.shop_id),
        "recipient_limit": None if page.is_platform else RECIPIENT_LIMIT,
        "history": history,
        "history_labels": history_labels(history),
        "schedule_labels": schedule_labels(history),
        "send_days": day_choices(),
        "status_labels": STATUS_LABELS,
        "media_labels": {int(row.id): media_label(row.media_kind) for row in history if row.media_kind},
        "caption_max": CAPTION_MAX,
        "request_token": new_request_token(),
    }


async def audience_count(session: AsyncSession, page: BroadcastPage, params: Mapping) -> JSONResponse:
    """How many people the audience in ``params`` reaches now; a rule violation is a 400 with a safe message."""
    try:
        spec = await checked_spec(session, spec_from_form(params, shop_id=page.shop_id), shop_id=page.shop_id)
    except ValueError as exc:
        return JSONResponse({"error": user_safe_error(exc)}, status_code=400)
    return JSONResponse({"count": await count_audience(session, spec, shop_id=page.shop_id), "label": spec.label})


async def media_from_form(form: Mapping, page: BroadcastPage) -> BroadcastMedia | None:
    """Store the photo or video chosen in the form, if any; raises ``ValueError`` with a safe reason."""
    upload = form.get(MEDIA_FIELD)
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        return None
    return await asyncio.to_thread(write_upload, data, shop_id=page.shop_id)


async def queue_from_form(
    session: AsyncSession, page: BroadcastPage, form: Mapping, *, actor: str, media: BroadcastMedia | None = None
) -> BroadcastLog:
    """Queue the composed message once (the request token makes a repeated submit return the same row)."""
    spec = spec_from_form(form, shop_id=page.shop_id)
    send_after = send_time_from_form(form)
    buttons = await broadcast_buttons(session, form_inputs(form), shop_id=page.shop_id)
    if page.is_platform:
        return await queue_broadcast(
            session,
            text=str(form.get("text") or ""),
            audience=spec,
            created_by=actor,
            request=BroadcastRequest(form.get("request_token"), buttons, send_after=send_after, media=media),
        )
    return await queue_shop_broadcast(
        session,
        shop_id=int(page.shop_id),
        text=form.get("text"),
        created_by=actor,
        request_key=form.get("request_token"),
        buttons=buttons,
        audience=spec,
        send_after=send_after,
        media=media,
    )


async def submit_form(session: AsyncSession, page: BroadcastPage, form: Mapping, *, actor: str) -> BroadcastLog:
    """Store the media, then queue; a rejected message leaves no stored file behind."""
    media = await media_from_form(form, page)
    try:
        return await queue_from_form(session, page, form, actor=actor, media=media)
    except ValueError:
        if media is not None:
            await asyncio.to_thread(delete_upload, media.file_name, shop_id=page.shop_id)
        raise


def staff_actor(staff: Mapping) -> str:
    return str(staff.get("username") or staff.get("bot_user_id") or staff.get("org_principal_id") or "staff")[:128]


def register_broadcast_routes(
    app: FastAPI,
    *,
    path: str,
    resolve: Callable[[dict], BroadcastPage],
    guard: Callable,
    render: Callable,
    get_db: Callable,
) -> None:
    """The routes of one group-message page; ``guard`` authenticates and ``resolve`` picks the tenant (or 403)."""

    @app.get(path)
    async def broadcast_page(request: Request, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)) -> Response:
        page = resolve(staff)
        context = await page_context(session, page)
        return render(
            request,
            TEMPLATE,
            {
                **context,
                "staff": staff,
                "campaigns_page": CAMPAIGNS_PATH if can_open_campaigns(staff) else "",
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.get(f"{path}/audience")
    async def broadcast_audience(request: Request, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)) -> Response:
        return await audience_count(session, resolve(staff), request.query_params)

    @app.post(path)
    async def broadcast_send(request: Request, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)) -> Response:
        page = resolve(staff)
        if not content_length_ok(request.headers.get("content-length"), MAX_BODY_BYTES):
            return redirect(page, err=TOO_LARGE)
        try:
            log = await submit_form(session, page, await request.form(), actor=staff_actor(staff))
        except ValueError as exc:
            await session.rollback()
            return redirect(page, err=user_safe_error(exc))
        return redirect(page, ok=queued_message(log))

    @app.post(f"{path}/history/clear")
    async def broadcast_history_clear(staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)) -> Response:
        page = resolve(staff)
        removed = await clear_broadcast_history(session, shop_id=page.shop_id)
        return redirect(page, ok=f"{removed} رکورد تاریخچه حذف شد")

    @app.post(f"{path}/history/{{log_id}}/delete")
    async def broadcast_history_delete(
        log_id: int, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)
    ) -> Response:
        page = resolve(staff)
        if not await delete_broadcast_log(session, log_id, shop_id=page.shop_id):
            return redirect(page, err="رکورد یافت نشد")
        return redirect(page, ok="رکورد حذف شد")
