"""«افزودن سرویس قبلی» on the customer's edit screen: preview and attach an existing panel service.

The subscription link is a bearer secret, so it travels only in a POST body, never in a URL, a
flash message or the page. The routes parse the form, call the service and render the answer.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from app.db.models import BotUser
from app.services.action_reasons import ReasonRequired, reason_from_form
from app.services.service_import import ImportOutcome
from app.services.service_import_assign import (
    AssignRefused,
    AssignRequest,
    AssignResult,
    assign_existing_service,
    parse_lookup,
    parse_panel_user_id,
    preview_existing_service,
)
from app.services.shop_scope import ShopScopeError

ASSIGN_PATH = "/users/{user_id}/import-service"
PREVIEW_PATH = ASSIGN_PATH + "/preview"
USER_NOT_FOUND = "کاربر یافت نشد"
ADDED_TEXT = "سرویس «{username}» به سرویس‌های این مشتری اضافه شد"
ALREADY_TEXT = "سرویس «{username}» از قبل در سرویس‌های این مشتری است"


def _edit_redirect(user_id: int, **params: str) -> RedirectResponse:
    return RedirectResponse(f"/users?{urlencode({'edit': int(user_id), **params})}", status_code=303)


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _wants_notice(form: FormData) -> bool:
    return str(form.get("notify") or "") == "1"


def _done_text(result: AssignResult) -> str:
    template = ALREADY_TEXT if result.outcome is ImportOutcome.ALREADY_YOURS else ADDED_TEXT
    return template.format(username=result.panel_username)


def register_service_import_assign_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(PREVIEW_PATH)
    async def service_import_assign_preview(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        user = await session.get(BotUser, int(user_id))
        if user is None:
            return _json_error(USER_NOT_FOUND, 404)
        form = await request.form()
        try:
            lookup = parse_lookup(form.get("kind"), form.get("value"))
            preview = await preview_existing_service(session, staff=staff, user=user, lookup=lookup)
        except PermissionError as exc:
            return _json_error(str(exc), 403)
        except ShopScopeError as exc:
            return _json_error(exc.message, 403)
        except AssignRefused as exc:
            return _json_error(str(exc), 409)
        return JSONResponse(preview.as_json())

    @app.post(ASSIGN_PATH)
    async def service_import_assign(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        user = await session.get(BotUser, int(user_id))
        if user is None:
            return RedirectResponse(f"/users?{urlencode({'err': USER_NOT_FOUND})}", status_code=303)
        form = await request.form()
        try:
            assign = AssignRequest(
                lookup=parse_lookup(form.get("kind"), form.get("value")),
                expected_panel_user_id=parse_panel_user_id(form.get("panel_user_id")),
                reason=reason_from_form(form),
                notify_customer=_wants_notice(form),
            )
            result = await assign_existing_service(session, staff=staff, user=user, request=assign)
        except (PermissionError, AssignRefused, ReasonRequired) as exc:
            return _edit_redirect(user_id, err=str(exc))
        except ShopScopeError as exc:
            return _edit_redirect(user_id, err=exc.message)
        return _edit_redirect(user_id, ok=_done_text(result))
