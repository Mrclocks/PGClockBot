"""JSON search over panel pages and settings for the sidebar search box."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from app.services.identity_chrome import hierarchy_identity
from app.services.marketing_nav import marketing_sections
from app.services.panel_nav import viewer_from_context
from app.services.panel_search import panel_search
from app.services.platform_identity import is_explicit_owner_staff
from app.services.settings_search import SEARCH_QUERY_MAX_LEN

SEARCH_PATH = "/panel-search"


def register_panel_search_pages(app: FastAPI, *, require_staff: Callable) -> None:
    @app.get(SEARCH_PATH)
    async def panel_search_json(
        q: str = Query("", max_length=SEARCH_QUERY_MAX_LEN),
        staff: dict = Depends(require_staff),
    ) -> JSONResponse:
        viewer = viewer_from_context(
            staff,
            hierarchy_identity(staff),
            is_owner=is_explicit_owner_staff(staff),
            marketing_sections=marketing_sections(staff),
        )
        hits = panel_search(viewer, q)
        return JSONResponse(
            {"results": [{"label": h.label, "where": h.where, "href": h.href} for h in hits]},
            headers={"Cache-Control": "no-store"},
        )
