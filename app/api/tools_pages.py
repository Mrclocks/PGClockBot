"""The «ابزار و پاکسازی» area: its list page and the owner's «سلامت سیستم» page."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.system_health import collect_health
from app.services.tools_nav import HEALTH_HREF, TOOLS_LIST_HREF, tool_sections, tools_hub_sections


def register_tools_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, require_admin: Callable, get_db: Callable
) -> None:
    @app.get(TOOLS_LIST_HREF, response_class=HTMLResponse)
    async def tools_list(request: Request, staff: dict = Depends(require_staff)) -> HTMLResponse:
        # Same visibility as the sidebar: a tool shows only when its own route would admit this staff member.
        sections = tool_sections(staff)
        if not sections:
            raise HTTPException(status_code=403, detail="دسترسی به ابزار و پاکسازی ندارید")
        return render(request, "tools.html", {"staff": staff, "tool_sections": sections, "hub": tools_hub_sections(sections)})

    @app.get(HEALTH_HREF, response_class=HTMLResponse)
    async def system_health_page(
        request: Request, staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)
    ) -> HTMLResponse:
        groups = await collect_health(session)
        return render(request, "system_health.html", {"staff": staff, "groups": groups})
