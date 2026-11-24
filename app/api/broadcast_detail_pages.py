"""The detail page of one group message in a tenant's history: its result and why people were not reached."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broadcast_pages import BroadcastPage, history_labels
from app.services.broadcast_failures import FAILURE_KINDS, KIND_UNKNOWN, broadcast_report
from app.services.broadcast_queue import STATUS_LABELS

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
    """``GET {path}/history/{id}``: the broadcast must belong to the tenant ``resolve`` picks, else 404."""

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
            },
        )
