"""JSON endpoints behind the «وضعیت اتصال» settings tab; Owner-only, the same services the bot uses."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.authz import authz_from_staff
from app.services.connection_monitor_access import MONITOR_ACCESS_DENIED, MonitorAccess
from app.services.connection_monitor_actions import edit_monitor_target, select_monitor_host
from app.services.connection_monitor_admin import active_monitor_hosts, set_monitor_enabled
from app.services.connection_monitor_overview import monitor_overview, safe_monitor_error
from app.services.connection_monitor_preferences import pause_monitor_target
from app.services.connection_monitor_queue import queue_manual_check
from app.services.connection_monitor_settings import set_monitor_account
from app.services.db_safe import rollback_quiet


logger = logging.getLogger(__name__)

BASE_PATH = "/connection-monitor"
INVALID_REQUEST = "درخواست نامعتبر است."
MAX_ID_DIGITS = 10
MAX_NAME_LENGTH = 128
EDIT_FIELDS = ("public_name", "display_order", "is_public")
FLAG_VALUES = {"1": True, "0": False}


def _digits(raw: object) -> int:
    text = str(raw)
    if not text.isascii() or not text.isdecimal() or len(text) > MAX_ID_DIGITS:
        raise ValueError(INVALID_REQUEST)
    return int(text)


def _edit_value(field: str, raw: object) -> object:
    if field == "public_name":
        return str(raw or "")[:MAX_NAME_LENGTH]
    if field == "display_order":
        return _digits(raw)
    if str(raw) not in FLAG_VALUES:
        raise ValueError(INVALID_REQUEST)
    return FLAG_VALUES[str(raw)]


def _failure(error: Exception) -> JSONResponse:
    message = safe_monitor_error(error)
    if message == MONITOR_ACCESS_DENIED:
        status = 403
    else:
        status = 409 if type(error) is ValueError else 503
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _access_for(staff: dict) -> MonitorAccess:
    return MonitorAccess(authz_from_staff(staff), True)


async def _guarded(session: AsyncSession, work: Callable[[], Awaitable[dict[str, Any]]]) -> JSONResponse:
    try:
        return JSONResponse(await work())
    except Exception as exc:  # last-resort boundary: roll back, log the type only, answer with a fixed text
        await rollback_quiet(session)
        logger.warning("Connection monitor web action failed error_type=%s", type(exc).__name__)
        return _failure(exc)


def _flag(form: Mapping[str, Any]) -> bool:
    value = str(form.get("enabled") or "")
    if value not in FLAG_VALUES:
        raise ValueError(INVALID_REQUEST)
    return FLAG_VALUES[value]


async def _edit_target(session: AsyncSession, access: MonitorAccess, target_id: int, form: Mapping[str, Any]) -> None:
    field = str(form.get("field") or "")
    if field not in EDIT_FIELDS:
        raise ValueError(INVALID_REQUEST)
    await edit_monitor_target(session, access, target_id=target_id, field=field, value=_edit_value(field, form.get("value")))


async def _target_action(session: AsyncSession, access: MonitorAccess, target_id: int, action: str,
                         form: Mapping[str, Any]) -> None:
    if action == "edit":
        await _edit_target(session, access, target_id, form)
    elif action == "check":
        await queue_manual_check(session, access, target_id=target_id, now=datetime.now(timezone.utc))
    elif action == "pause":
        await pause_monitor_target(session, access, target_id=target_id)
    else:
        raise ValueError(INVALID_REQUEST)


async def _host_choices(session: AsyncSession, access: MonitorAccess, after_id: int) -> dict[str, Any]:
    page = await active_monitor_hosts(session, access, after_id=after_id)
    return {"ok": True, "hosts": [{"id": item.host_id, "name": item.name} for item in page.choices],
            "next_id": page.next_host_id}


def register_connection_monitor_pages(app: FastAPI, *, require_admin: Callable, get_db: Callable) -> None:
    @app.get(BASE_PATH + "/status")
    async def monitor_status(request: Request, staff: dict = Depends(require_admin),
                             session: AsyncSession = Depends(get_db)) -> JSONResponse:
        return await _guarded(session, lambda: monitor_overview(
            session, _access_for(staff), now=datetime.now(timezone.utc),
            after_id=_digits(request.query_params.get("after_id", "0")),
            verify_core=request.query_params.get("verify") == "1",
        ))

    @app.post(BASE_PATH + "/enabled")
    async def monitor_enabled(request: Request, staff: dict = Depends(require_admin),
                              session: AsyncSession = Depends(get_db)) -> JSONResponse:
        form = await request.form()

        async def work() -> dict[str, Any]:
            await set_monitor_enabled(session, _access_for(staff), enabled=_flag(form))
            return {"ok": True}
        return await _guarded(session, work)

    @app.post(BASE_PATH + "/account")
    async def monitor_account(request: Request, staff: dict = Depends(require_admin),
                              session: AsyncSession = Depends(get_db)) -> JSONResponse:
        form = await request.form()

        async def work() -> dict[str, Any]:
            username = str(form.get("username") or "")[:MAX_NAME_LENGTH + 1]
            await set_monitor_account(session, _access_for(staff), username=username)
            return {"ok": True}
        return await _guarded(session, work)

    @app.get(BASE_PATH + "/hosts")
    async def monitor_hosts(request: Request, staff: dict = Depends(require_admin),
                            session: AsyncSession = Depends(get_db)) -> JSONResponse:
        return await _guarded(session, lambda: _host_choices(
            session, _access_for(staff), _digits(request.query_params.get("after_id", "0"))))

    @app.post(BASE_PATH + "/targets")
    async def monitor_select(request: Request, staff: dict = Depends(require_admin),
                             session: AsyncSession = Depends(get_db)) -> JSONResponse:
        form = await request.form()

        async def work() -> dict[str, Any]:
            await select_monitor_host(session, _access_for(staff), host_id=_digits(form.get("host_id")))
            return {"ok": True}
        return await _guarded(session, work)

    @app.post(BASE_PATH + "/targets/{target_id}/{action}")
    async def monitor_target_action(target_id: int, action: str, request: Request, staff: dict = Depends(require_admin),
                                    session: AsyncSession = Depends(get_db)) -> JSONResponse:
        form = await request.form()

        async def work() -> dict[str, Any]:
            await _target_action(session, _access_for(staff), target_id, action, form)
            return {"ok": True}
        return await _guarded(session, work)
