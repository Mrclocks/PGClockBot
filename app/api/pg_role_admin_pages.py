"""«ادمین‌ها» page for staff whose own PasarGuard role allows managing admins."""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pg_pages import _pg_ctx, _staff_pg
from app.services.numbers import parse_optional_int
from app.services.pasarguard import PasarGuardError
from app.services.pg_role_admins import (
    CHANGE_PERMISSION,
    ROLE_ADMINS_FEATURE,
    RoleAdminError,
    RoleAdminsPage,
    admin_actions_for_staff,
    change_role_admin,
    create_role_admin,
    load_role_admins_page,
)

logger = logging.getLogger(__name__)

PAGE_PATH = "/pg/role-admins"
PANEL_ERROR_MSG = "ارتباط با پاسارگارد برقرار نشد؛ چند لحظهٔ دیگر دوباره تلاش کنید."
DONE_MESSAGES = {
    "disable": "ادمین غیرفعال شد",
    "enable": "ادمین فعال شد",
    "reset_usage": "مصرف ادمین صفر شد",
    "delete": "ادمین حذف شد",
}


def _back(*, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"?err={quote(err)}" if err else (f"?ok={quote(ok)}" if ok else "")
    return RedirectResponse(f"{PAGE_PATH}{query}", status_code=303)


def _panel_error(exc: Exception) -> str:
    if isinstance(exc, PasarGuardError):
        return exc.user_message(fallback=PANEL_ERROR_MSG)
    return PANEL_ERROR_MSG


def register_pg_role_admin_pages(
    app: FastAPI, *, render: Callable, require_pg_perm: Callable, get_db: Callable
) -> None:
    require_feature = require_pg_perm(ROLE_ADMINS_FEATURE)

    @app.get(PAGE_PATH, response_class=HTMLResponse)
    async def pg_role_admins(
        request: Request,
        staff: dict = Depends(require_feature),
        session: AsyncSession = Depends(get_db),
    ) -> HTMLResponse:
        page = RoleAdminsPage(rows=(), roles=(), truncated=False)
        err = request.query_params.get("err") or ""
        try:
            client, _as_owner = await _staff_pg(session, staff)
            page = await load_role_admins_page(session, client, staff)
        except RoleAdminError as exc:
            err = str(exc)
        except (PasarGuardError, OSError) as exc:
            logger.warning("role admins list failed", exc_info=True)
            err = _panel_error(exc)
        return render(
            request,
            "pg_role_admins.html",
            _pg_ctx(
                staff,
                page=page,
                admin_actions=admin_actions_for_staff(staff),
                flash_err=err,
                flash_ok=request.query_params.get("ok") or "",
                active=ROLE_ADMINS_FEATURE,
            ),
        )

    @app.post(PAGE_PATH)
    async def pg_role_admins_create(
        username: str = Form(...),
        password: str = Form(...),
        role_id: str = Form(""),
        staff: dict = Depends(require_feature),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            chosen_role = parse_optional_int(role_id)
        except ValueError:
            chosen_role = None
        try:
            client, _as_owner = await _staff_pg(session, staff)
            created = await create_role_admin(
                client, staff, username=username, password=password, role_id=chosen_role
            )
        except RoleAdminError as exc:
            return _back(err=str(exc))
        except (PasarGuardError, OSError) as exc:
            logger.warning("role admin create failed", exc_info=True)
            return _back(err=_panel_error(exc))
        return _back(ok=f"ادمین «{created}» ساخته شد")

    @app.post(PAGE_PATH + "/{username}/{change}")
    async def pg_role_admins_change(
        username: str,
        change: str,
        staff: dict = Depends(require_feature),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        if change not in CHANGE_PERMISSION:
            return _back(err="درخواست نامعتبر است")
        try:
            client, _as_owner = await _staff_pg(session, staff)
            await change_role_admin(session, client, staff, username=username, change=change)  # type: ignore[arg-type]  # validated against CHANGE_PERMISSION above
        except RoleAdminError as exc:
            return _back(err=str(exc))
        except (PasarGuardError, OSError) as exc:
            logger.warning("role admin %s failed", change, exc_info=True)
            return _back(err=_panel_error(exc))
        return _back(ok=DONE_MESSAGES[change])
