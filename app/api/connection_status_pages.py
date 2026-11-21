"""Web controls for connection tests using the same services as the main bot."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.authz import authz_from_staff
from app.services.connection_monitor_access import MonitorAccess, require_monitor_owner
from app.services.connection_monitor_actions import MonitorTargetEdit, select_monitor_config, update_monitor_target
from app.services.connection_monitor_admin import monitor_config_choices, set_monitor_enabled
from app.services.connection_monitor_errors import monitor_error_message
from app.services.connection_monitor_preferences import delete_monitor_target, pause_monitor_target
from app.services.connection_monitor_queue import queue_manual_check
from app.services.connection_monitor_settings import set_monitor_account
from app.services.connection_monitor_timing import INTERVAL_CHOICES, set_monitor_interval
from app.services.connection_monitor_views import monitor_admin_page
from app.services.connection_status_texts import PLAIN_STATUS_LABELS, REASON_LABELS, connection_check_time
from app.services.db_safe import rollback_quiet
from app.services.pasarguard import PasarGuardError


logger = logging.getLogger(__name__)
STATUS_PATH = "/connection-status"
EXPECTED_ERRORS = (ValueError, PasarGuardError, httpx.HTTPError, SQLAlchemyError)
BoundedId = Annotated[int, Query(ge=0, le=2147483647)]


async def _action_result(session: AsyncSession, operation: Awaitable[object]) -> RedirectResponse:
    try:
        await operation
    except EXPECTED_ERRORS as exc:
        await rollback_quiet(session)
        logger.warning("Connection web action failed type=%s reason=%s", type(exc).__name__, getattr(exc, "code", "action_failed"))
        return RedirectResponse(STATUS_PATH + "?" + urlencode({"error": monitor_error_message(exc)}), status_code=303)
    return RedirectResponse(STATUS_PATH + "?saved=1", status_code=303)


def register_connection_status_pages(app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable) -> None:
    async def require_access(staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)) -> MonitorAccess:
        access = MonitorAccess(authz_from_staff(staff), True)
        try:
            await require_monitor_owner(session, access)
        except ValueError:
            raise HTTPException(403, "دسترسی به تنظیمات اتصال ندارید.") from None
        await session.commit()
        return access

    @app.get(STATUS_PATH)
    async def connection_page(request: Request, after: BoundedId = 0, host_after: BoundedId = 0,
                              access: MonitorAccess = Depends(require_access), staff: dict = Depends(require_staff),
                              session: AsyncSession = Depends(get_db)) -> Response:
        page = await monitor_admin_page(session, access, now=datetime.now(timezone.utc), after_id=after)
        hosts, host_error = None, None
        if request.query_params.get("hosts") == "1":
            try:
                hosts = await monitor_config_choices(session, access, offset=host_after)
            except EXPECTED_ERRORS as exc:
                await rollback_quiet(session)
                logger.warning("Connection host list failed type=%s reason=%s", type(exc).__name__, getattr(exc, "code", "catalog_failed"))
                host_error = monitor_error_message(exc)
        deployment = get_settings()
        return render(request, "connection_status.html", {
            "staff": staff, "page": page, "hosts": hosts, "host_error": host_error,
            "status_labels": PLAIN_STATUS_LABELS, "reason_labels": REASON_LABELS,
            "checked_time": connection_check_time,
            "binary_configured": bool(deployment.connection_monitor_xray_path and deployment.connection_monitor_xray_sha256),
            "interval_choices": INTERVAL_CHOICES,
        })

    @app.post(STATUS_PATH + "/account")
    async def connection_account(username: Annotated[str, Form()],
                                 access: MonitorAccess = Depends(require_access), session: AsyncSession = Depends(get_db)) -> Response:
        return await _action_result(session, set_monitor_account(session, access, username=username.strip()))

    @app.post(STATUS_PATH + "/runtime")
    async def connection_runtime(enabled: Annotated[Literal["0", "1"], Form()],
                                 access: MonitorAccess = Depends(require_access), session: AsyncSession = Depends(get_db)) -> Response:
        return await _action_result(session, set_monitor_enabled(session, access, enabled=enabled == "1"))

    @app.post(STATUS_PATH + "/interval")
    async def connection_interval(seconds: Annotated[int, Form(ge=1, le=86400)],
                                  access: MonitorAccess = Depends(require_access), session: AsyncSession = Depends(get_db)) -> Response:
        return await _action_result(session, set_monitor_interval(session, access, seconds=seconds))

    @app.post(STATUS_PATH + "/configs/{config_key}")
    async def connection_select(config_key: str, access: MonitorAccess = Depends(require_access),
                                session: AsyncSession = Depends(get_db)) -> Response:
        return await _action_result(session, select_monitor_config(session, access, config_key=config_key))

    @app.post(STATUS_PATH + "/targets/{target_id}")
    async def connection_target(target_id: int, display_order: Annotated[int, Form(ge=0, le=2147483647)],
                                is_public: Annotated[Literal["0", "1"], Form()],
                                public_name: Annotated[str, Form(max_length=64)] = "",
                                access: MonitorAccess = Depends(require_access), session: AsyncSession = Depends(get_db)) -> Response:
        try:
            changes = MonitorTargetEdit(public_name=public_name, display_order=display_order, is_public=is_public == "1")
        except ValidationError:
            raise HTTPException(422, "نام یا ترتیب نمایش معتبر نیست.") from None
        return await _action_result(session, update_monitor_target(session, access, target_id=target_id, changes=changes))

    @app.post(STATUS_PATH + "/targets/{target_id}/{action}")
    async def connection_target_action(target_id: int, action: Literal["pause", "check", "delete"],
                                       access: MonitorAccess = Depends(require_access), session: AsyncSession = Depends(get_db)) -> Response:
        if action == "pause":
            operation = pause_monitor_target(session, access, target_id=target_id)
        elif action == "delete":
            operation = delete_monitor_target(session, access, target_id=target_id)
        else:
            operation = queue_manual_check(session, access, target_id=target_id, now=datetime.now(timezone.utc))
        return await _action_result(session, operation)
