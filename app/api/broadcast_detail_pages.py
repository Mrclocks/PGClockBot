"""The detail page of one group message in a tenant's history: its result, why people were not reached, and a resend."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broadcast_pages import BroadcastPage, history_labels, redirect, staff_actor
from app.services.broadcast_failures import FAILURE_KINDS, KIND_UNKNOWN, broadcast_report
from app.services.broadcast_queue import STATUS_LABELS
from app.services.broadcast_retry import queue_retry, retry_offer
from app.services.redact import user_safe_error

DETAIL_TEMPLATE = "broadcast_detail.html"


def register_broadcast_detail_routes(
    app: FastAPI,
    *,
    path: str,
    resolve: Callable[[dict], BroadcastPage],
    guard: Callable,
    render: Callable,
    get_db: Callable,
) -> None:
    """``{path}/history/{id}`` and its resend: the broadcast must belong to the tenant ``resolve`` picks."""

    @app.get(f"{path}/history/{{log_id}}")
    async def broadcast_detail(
        log_id: int, request: Request, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)
    ) -> Response:
        page = resolve(staff)
        report = await broadcast_report(session, log_id, shop_id=page.shop_id)
        if report is None:
            raise HTTPException(404, "این پیام پیدا نشد")
        return render(
            request,
            DETAIL_TEMPLATE,
            {
                "staff": staff,
                "page_path": page.path,
                "report": report,
                "audience_label": history_labels([report.log])[int(report.log.id)],
                "status_labels": STATUS_LABELS,
                "unknown_kind": FAILURE_KINDS[KIND_UNKNOWN],
                "retry": await retry_offer(session, report.log),
            },
        )

    @app.post(f"{path}/history/{{log_id}}/retry")
    async def broadcast_retry(
        log_id: int, request: Request, staff: dict = Depends(guard), session: AsyncSession = Depends(get_db)
    ) -> Response:
        page = resolve(staff)
        form = await request.form()
        try:
            resend = await queue_retry(
                session,
                log_id,
                shop_id=page.shop_id,
                include_unknown=form.get("include_unknown") == "1",
                created_by=staff_actor(staff),
            )
        except ValueError as exc:
            await session.rollback()
            return redirect(page, err=user_safe_error(exc))
        return redirect(page, ok=f"ارسال دوباره در صف قرار گرفت: {resend.total} مخاطب")
