"""Scheduler job: finish freeze steps left half-done and resume frozen services at their deadline."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.models import BotUser, ServiceFreeze, UserService
from app.db.session import SessionLocal
from app.services.customer_notice_queue import add_notice
from app.services.service_freeze import process_due_freezes
from app.services.service_freeze_settings import RESUMED_ACTION

logger = logging.getLogger(__name__)


async def _queue_resumed_notices(session, resumed: list[ServiceFreeze]) -> None:
    """Tell each customer their service is running again; one query for users and services."""
    if not resumed:
        return
    users = {u.id: u for u in await session.scalars(select(BotUser).where(BotUser.id.in_({r.user_id for r in resumed})))}
    services = {s.id: s for s in await session.scalars(select(UserService).where(UserService.id.in_({r.service_id for r in resumed})))}
    for row in resumed:
        user, service = users.get(row.user_id), services.get(row.service_id)
        if user is None or service is None or user.is_blocked:
            continue
        add_notice(
            session, shop_id=row.shop_id, bot_user_id=int(user.id), telegram_id=int(user.telegram_id),
            action=RESUMED_ACTION, service_name=service.pg_username or "", reason="", service_id=int(service.id),
        )
    await session.commit()


async def run_service_freezes() -> None:
    """One pass; a failed pass is logged and retried on the next run."""
    try:
        async with SessionLocal() as session:
            resumed = await process_due_freezes(session)
            await _queue_resumed_notices(session, resumed)
    except Exception:  # noqa: BLE001 - background job of last resort; the rows keep their state
        logger.exception("service freeze pass failed")
