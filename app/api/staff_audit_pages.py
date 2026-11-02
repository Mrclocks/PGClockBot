"""The staff action log page: one shop's records, newest first, keyset-paginated."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.staff_audit import PAGE_SIZE, list_staff_actions, staff_action_views, staff_audit_scope

STAFF_AUDIT_PATH = "/staff-actions"
# Ids are bounded by the column range; anything else is ignored rather than trusted.
MAX_ID = 2**31 - 1


def _before_id(raw: str | None) -> int | None:
    if not raw or not raw.isdigit():
        return None
    value = int(raw)
    return value if 0 < value <= MAX_ID else None


def register_staff_audit_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable,
) -> None:
    @app.get(STAFF_AUDIT_PATH)
    async def staff_actions_page(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        try:
            shop_id = staff_audit_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        rows = await list_staff_actions(
            session, shop_id=shop_id, before_id=_before_id(request.query_params.get("before"))
        )
        next_before = rows[-1].id if len(rows) == PAGE_SIZE else None
        return render(
            request,
            "staff_actions.html",
            {"staff": staff, "actions": staff_action_views(rows), "next_before": next_before},
        )
