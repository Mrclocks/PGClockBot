"""Decide requests to add an existing service when «تأیید ادمین» is on.

Approving re-checks everything against the panel at that moment (the service may have moved to
another admin or customer since it was requested) and only then attaches it. The decision is
claimed with one atomic UPDATE, so two admins clicking at once cannot both attach it; the panel is
only read, so a failed or interrupted approval can safely be tried again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ServiceImportRequest
from app.services.pasarguard import PasarGuardError, subscription_path_from_url, user_subscription_url
from app.services.service_import import (
    GENERIC_REFUSAL,
    PANEL_UNAVAILABLE,
    Reason,
    ServiceImportError,
    VerifiedService,
    panel_user_id,
    allowed_admins,
    attach_verified,
    panel_client,
    parse_subscription_link,
)
from app.services.service_import_config import read_settings
from app.services.service_import_store import (
    STATUS_APPROVED,
    STATUS_APPROVING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    as_utc,
)
from app.services.pg_user_scope import pg_user_owner_username
from app.services.shop_membership import is_shop_customer
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

APPROVAL_LEASE = timedelta(minutes=5)
LIST_LIMIT = 50
NOT_PENDING = "این درخواست پیدا نشد یا قبلاً بررسی شده است."


@dataclass(frozen=True, slots=True)
class Decision:
    request_id: int
    user_id: int
    approved: bool
    service_id: int | None = None
    shop_id: int | None = None  # the shop the request was made in (None = platform)


def _in_scope(scope: int | None) -> ColumnElement[bool]:
    return ServiceImportRequest.shop_id.is_(None) if scope is None else ServiceImportRequest.shop_id == int(scope)


async def pending_count(session: AsyncSession, scope: int | None) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(ServiceImportRequest).where(
                _in_scope(scope),
                ServiceImportRequest.status == STATUS_PENDING,
                ServiceImportRequest.expires_at > datetime.now(timezone.utc),
            )
        )
        or 0
    )


async def list_requests(session: AsyncSession, scope: int | None, *, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """Open requests of one shop (or the platform), oldest first, with the customer's chat id."""
    rows = await session.execute(
        select(ServiceImportRequest, BotUser.telegram_id, BotUser.full_name)
        .join(BotUser, BotUser.id == ServiceImportRequest.bot_user_id)
        .where(
            _in_scope(scope),
            ServiceImportRequest.status == STATUS_PENDING,
            ServiceImportRequest.expires_at > datetime.now(timezone.utc),
        )
        .order_by(ServiceImportRequest.id)
        .limit(int(limit))
    )
    return [
        {
            "id": int(req.id),
            "customer": int(telegram_id),
            "customer_name": full_name or "",
            "panel_username": req.panel_username,
            "created_at": as_utc(req.created_at).isoformat() if req.created_at else "",
            "expires_at": as_utc(req.expires_at).isoformat(),
        }
        for req, telegram_id, full_name in rows.all()
    ]


async def _claim(session: AsyncSession, request_id: int, scope: int | None, decided_by: str) -> ServiceImportRequest:
    now = datetime.now(timezone.utc)
    stale = now - APPROVAL_LEASE
    result = await session.execute(
        update(ServiceImportRequest)
        .where(
            ServiceImportRequest.id == int(request_id),
            _in_scope(scope),
            or_(
                and_(ServiceImportRequest.status == STATUS_PENDING, ServiceImportRequest.expires_at > now),
                and_(ServiceImportRequest.status == STATUS_APPROVING, ServiceImportRequest.decided_at < stale),
            ),
        )
        .values(status=STATUS_APPROVING, decided_by=decided_by[:64], decided_at=now),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    if result.rowcount != 1:
        raise ValueError(NOT_PENDING)
    return await session.scalar(
        select(ServiceImportRequest).where(ServiceImportRequest.id == int(request_id)).execution_options(populate_existing=True)
    )


async def _settle(session: AsyncSession, request_id: int, status: str, reason: str | None = None) -> None:
    await session.rollback()
    values: dict[str, Any] = {"status": status, "reason": reason}
    try:
        await session.execute(
            update(ServiceImportRequest)
            .where(ServiceImportRequest.id == int(request_id), ServiceImportRequest.status == STATUS_APPROVING)
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        await session.commit()
    except IntegrityError:
        # The customer asked again meanwhile; this copy can no longer be reopened.
        await session.rollback()
        await session.execute(
            update(ServiceImportRequest)
            .where(ServiceImportRequest.id == int(request_id), ServiceImportRequest.status == STATUS_APPROVING)
            .values(status=STATUS_FAILED, reason=reason),
            execution_options={"synchronize_session": False},
        )
        await session.commit()


async def _reverify(session: AsyncSession, request: ServiceImportRequest, user: BotUser) -> tuple[VerifiedService, Any]:
    shop_id = request.shop_id
    if not await is_shop_customer(session, user, shop_id):
        raise ServiceImportError(GENERIC_REFUSAL, Reason.WRONG_SHOP)
    if user.is_blocked:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.BLOCKED)
    config = read_settings(await get_all_settings(session, reseller_id=shop_id))
    pg = await panel_client(session, shop_id)
    admins = await allowed_admins(session, pg, shop_id, config)
    if not admins:
        raise ServiceImportError(PANEL_UNAVAILABLE, Reason.IDENTITY_UNKNOWN)
    record = await pg.get_user_by_username(request.panel_username)
    if not isinstance(record, dict) or panel_user_id(record) != int(request.panel_user_id):
        raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND)
    owner = pg_user_owner_username(record)
    if owner not in admins:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.ADMIN_NOT_ALLOWED, detail={"owner_admin": owner})
    sub_url = user_subscription_url(record)
    link = parse_subscription_link(sub_url)
    if link is None:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND)
    prefix = subscription_path_from_url(sub_url) or ""
    return VerifiedService(record=record, token=link.token, prefix=prefix, panel_id=int(request.panel_user_id)), config


async def approve_request(session: AsyncSession, request_id: int, scope: int | None, *, decided_by: str) -> Decision:
    """Attach the requested service to its customer. Raises ``ValueError`` / ``ServiceImportError``."""
    request = await _claim(session, request_id, scope, decided_by)
    user_id = int(request.bot_user_id)
    try:
        user = await session.get(BotUser, user_id)
        if user is None:
            raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND)
        verified, config = await _reverify(session, request, user)
        result = await attach_verified(
            session, user, verified, shop_id=request.shop_id, renewable=True, config=config, ask_approval=False
        )
    except ServiceImportError as exc:
        await _settle(session, request_id, STATUS_FAILED, exc.reason.value)
        raise
    except (PasarGuardError, httpx.HTTPError, TimeoutError):
        # Only reads reached the panel, so the admin can simply try again.
        logger.warning("service import approval: panel call failed request=%s", request_id, exc_info=True)
        await _settle(session, request_id, STATUS_PENDING, Reason.PANEL_ERROR.value)
        raise ServiceImportError(PANEL_UNAVAILABLE, Reason.PANEL_ERROR) from None
    service_id = int(result.service.id) if result.service is not None else None  # read before _settle expires it
    await _settle(session, request_id, STATUS_APPROVED)
    return Decision(request_id=int(request_id), user_id=user_id, approved=True, service_id=service_id, shop_id=scope)


async def reject_request(session: AsyncSession, request_id: int, scope: int | None, *, decided_by: str) -> Decision:
    now = datetime.now(timezone.utc)
    user_id = await session.scalar(
        select(ServiceImportRequest.bot_user_id).where(ServiceImportRequest.id == int(request_id), _in_scope(scope))
    )
    result = await session.execute(
        update(ServiceImportRequest)
        .where(
            ServiceImportRequest.id == int(request_id),
            _in_scope(scope),
            ServiceImportRequest.status == STATUS_PENDING,
        )
        .values(status=STATUS_REJECTED, decided_by=decided_by[:64], decided_at=now),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    if result.rowcount != 1 or user_id is None:
        raise ValueError(NOT_PENDING)
    return Decision(request_id=int(request_id), user_id=int(user_id), approved=False, shop_id=scope)
