"""Marketing page listing the customers who took the free trial and whether they bought."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.authz import authz_from_staff, can_shop
from app.services.db_safe import recover_session
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
from app.services.trial_users import (
    FILTER_LABELS,
    list_trial_users,
    normalize_filter,
    trial_summary,
)

TRIAL_USERS_PATH = "/trial-users"
VIEW_PERMISSION = "trial_users"
MANAGE_PERMISSION = "trial_users_manage"
SETTINGS_PERMISSION = "shop_settings"
OWNER_SETTINGS_HREF = "/settings?tab=limits"
SHOP_SETTINGS_HREF = "/shop-settings?tab=limits"


def trial_users_scope(staff: dict, permission: str = VIEW_PERMISSION) -> int | None:
    """The shop whose trial takers this staff member may handle; 403 when none."""
    if not can_shop(authz_from_staff(staff), permission):
        raise HTTPException(403)
    try:
        return resolve_shop_scope_id(staff)
    except ShopScopeError:
        raise HTTPException(403) from None


def trial_settings_href(staff: dict) -> str | None:
    """Where the trial settings (the «اکانت تست» group of «محدودیت») live for this staff member."""
    if staff.get("role") == "admin":
        return OWNER_SETTINGS_HREF
    return SHOP_SETTINGS_HREF if can_shop(authz_from_staff(staff), SETTINGS_PERMISSION) else None


def _parse_cursor(raw: str | None) -> int | None:
    text = str(raw or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 12 else None


def register_trial_users_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable,
) -> None:
    @app.get(TRIAL_USERS_PATH)
    async def trial_users_page(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        shop_id = trial_users_scope(staff)
        await recover_session(session)
        params = request.query_params
        trial_filter = normalize_filter(params.get("filter"))
        search = (params.get("q") or "").strip()
        page = await list_trial_users(
            session,
            staff=staff,
            shop_id=shop_id,
            trial_filter=trial_filter,
            search=search,
            before=_parse_cursor(params.get("before")),
        )
        summary = await trial_summary(session, shop_id=shop_id)
        return render(
            request,
            "trial_users.html",
            {
                "staff": staff,
                "rows": page.rows,
                "next_before": page.next_before,
                "summary": summary,
                "filters": FILTER_LABELS,
                "active_filter": trial_filter,
                "q": search,
                "settings_href": trial_settings_href(staff),
                "flash_ok": params.get("ok"),
                "flash_err": params.get("err"),
            },
        )
