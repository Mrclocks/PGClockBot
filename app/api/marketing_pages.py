"""The marketing area's list page: every marketing section the staff member may open, grouped by topic."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.services.marketing_nav import marketing_hub_sections, marketing_sections
from app.services.panel_nav import MARKETING_LIST_HREF


def register_marketing_pages(app: FastAPI, *, render: Callable, require_staff: Callable) -> None:
    @app.get(MARKETING_LIST_HREF, response_class=HTMLResponse)
    async def marketing_list(request: Request, staff: dict = Depends(require_staff)) -> HTMLResponse:
        # Same visibility as the sidebar: a section shows only when its own route would admit this staff member.
        sections = marketing_sections(staff)
        if not sections:
            raise HTTPException(status_code=403, detail="دسترسی به بازاریابی ندارید")
        return render(
            request,
            "marketing.html",
            {"staff": staff, "marketing_sections": sections, "hub": marketing_hub_sections(sections)},
        )
