"""A customer changes the subscription link of their own service from «⚙️ بیشتر».

The change is claimed in the database (one open change per service) and committed before the panel
is called. When the panel answers, the new link is saved on the service. When its answer is lost,
the change becomes ``unknown`` and is never revoked again: the panel user is read and the link is
compared with the old token's hash. A changed link is saved and the change is done; a link still
unchanged once the answer can no longer arrive means the revoke did not happen, and the change fails
without counting against the customer's limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ServiceLinkReset, UserService
from app.services.pasarguard import PasarGuardError, extract_sub_token, get_pg, get_pg_for_reseller
from app.services.service_freeze import can_be_frozen, has_open_order, is_on_paid_plan, open_freeze
from app.services.service_link_reset_settings import (
    COOLDOWN,
    JOB_BATCH,
    OPEN_STATUSES,
    PERIOD,
    SETTLE_AFTER,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_UNKNOWN,
    LinkResetPolicy,
    link_reset_policy,
)
from app.services.service_scope import service_shop_id
from app.services.service_transfer import open_transfer
from app.services.subscription_link_store import (
    PANEL_READ_ERRORS,
    LinkScope,
    PanelLink,
    link_after_revoke,
    read_panel_link,
    save_links,
)

logger = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 15.0
# The panel answered and refused: nothing changed. 408 and 429 only say the request did not get through yet.
UNSURE_HTTP_STATUSES = frozenset({408, 429})
NOT_FOUND = "سرویس یافت نشد"
NOT_OFFERED = "تغییر لینک برای این سرویس فعال نیست"
NO_LINK = "لینک اشتراک این سرویس ثبت نشده است؛ به پشتیبانی خبر دهید"
MISSING_ON_PANEL = "این سرویس روی سرور پیدا نشد؛ به پشتیبانی خبر دهید"
FROZEN = "سرویس متوقف است؛ اول آن را از حالت توقف خارج کنید"
OPEN_TRANSFER = "یک لینک انتقال باز روی این سرویس هست؛ اول آن را لغو کنید"
OPEN_ORDER = "یک سفارش باز روی این سرویس هست؛ بعد از پایان یا لغو آن دوباره تلاش کنید"
LIMIT_REACHED = "سقف تغییر لینک این سرویس در ۳۰ روز گذشته پر شده است"
TOO_SOON = "لینک این سرویس همین تازگی عوض شده؛ چند دقیقهٔ دیگر دوباره تلاش کنید"
IN_PROGRESS = "تغییر لینک این سرویس در حال انجام است؛ چند دقیقهٔ دیگر «لینک و QR» را ببینید"
PANEL_DOWN = "پنل الان پاسخ نداد؛ چند دقیقهٔ دیگر دوباره امتحان کنید"
REFUSED = "سرور اجازهٔ تغییر لینک این سرویس را نداد؛ به پشتیبانی خبر دهید"


@dataclass(frozen=True, slots=True)
class LinkResetOutcome:
    """``done`` with the new link, or ``unknown`` while the job finds out what the panel did."""

    status: str
    service_id: int
    link: PanelLink | None = None


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class OpenReset:
    """The values of a change that the panel steps need; read once, so a rollback cannot expire them."""

    id: int
    service_id: int
    shop_id: int | None
    pg_user_id: int
    old_token_hash: str = field(repr=False)
    status: str
    created_at: datetime

    @classmethod
    def of(cls, row: ServiceLinkReset) -> OpenReset:
        return cls(int(row.id), row.service_id, row.shop_id, row.pg_user_id, row.old_token_hash,
                   row.status, _as_utc(row.created_at))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _panel(session: AsyncSession, shop_id: int | None) -> Any:
    return await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()


async def link_reset_button(session: AsyncSession, service: UserService, ui: Mapping[str, Any]) -> bool:
    """Whether «⚙️ بیشتر» offers the change: a paid service of a chosen plan that the bot sold."""
    if not can_be_frozen(service) or not link_reset_policy(ui).allows_plan(service.plan_id):
        return False
    return await is_on_paid_plan(session, service)


async def changes_left(session: AsyncSession, service_id: int, policy: LinkResetPolicy, now: datetime) -> int:
    """Changes the service may still make in the current 30 days; only finished changes count."""
    used = await session.scalar(
        select(func.count(ServiceLinkReset.id)).where(
            ServiceLinkReset.service_id == int(service_id), ServiceLinkReset.status == STATUS_DONE,
            ServiceLinkReset.created_at >= now - PERIOD,
        )
    )
    return max(policy.per_period - int(used or 0), 0)


async def _locked_service(session: AsyncSession, user: BotUser, service_id: int, shop_id: int | None) -> UserService:
    service = await session.scalar(
        select(UserService).where(UserService.id == int(service_id)).with_for_update()
        .execution_options(populate_existing=True)
    )
    if service is None or int(service.bot_user_id) != int(user.id) or user.is_blocked:
        raise ValueError(NOT_FOUND)
    if await service_shop_id(session, service) != shop_id:
        raise ValueError(NOT_FOUND)
    if not can_be_frozen(service):
        raise ValueError(NOT_OFFERED)
    return service


async def _assert_resettable(
    session: AsyncSession, service: UserService, policy: LinkResetPolicy, now: datetime
) -> None:
    if not policy.allows_plan(service.plan_id) or not await is_on_paid_plan(session, service):
        raise ValueError(NOT_OFFERED)
    if service.panel_missing_at is not None:
        raise ValueError(MISSING_ON_PANEL)
    if await open_freeze(session, int(service.id)) is not None:
        raise ValueError(FROZEN)
    transfer = await open_transfer(session, int(service.id))
    if transfer is not None and _as_utc(transfer.expires_at) > now:
        raise ValueError(OPEN_TRANSFER)
    # A delivery still in flight writes the service's link from its own panel answer.
    if await has_open_order(session, int(service.id)):
        raise ValueError(OPEN_ORDER)


async def _assert_within_limits(session: AsyncSession, service_id: int, policy: LinkResetPolicy, now: datetime) -> None:
    open_change = await session.scalar(
        select(ServiceLinkReset.id).where(
            ServiceLinkReset.service_id == int(service_id), ServiceLinkReset.status.in_(OPEN_STATUSES),
        ).limit(1)
    )
    if open_change is not None:
        raise ValueError(IN_PROGRESS)
    if await changes_left(session, service_id, policy, now) <= 0:
        raise ValueError(LIMIT_REACHED)
    last = await session.scalar(
        select(func.max(ServiceLinkReset.created_at)).where(
            ServiceLinkReset.service_id == int(service_id), ServiceLinkReset.status != STATUS_FAILED,
        )
    )
    if last is not None and _as_utc(last) > now - COOLDOWN:
        raise ValueError(TOO_SOON)


def _current_token(service: UserService) -> str:
    token = (service.subscription_token or "").strip() or extract_sub_token(service.subscription_url)
    if not token:
        raise ValueError(NO_LINK)
    return token


async def _claim(
    session: AsyncSession, user: BotUser, service_id: int, *,
    shop_id: int | None, policy: LinkResetPolicy, now: datetime,
) -> OpenReset:
    """Check every rule on the locked service and commit a ``pending`` change before the panel call."""
    try:
        service = await _locked_service(session, user, service_id, shop_id)
        await _assert_resettable(session, service, policy, now)
        await _assert_within_limits(session, int(service.id), policy, now)
        row = ServiceLinkReset(
            service_id=int(service.id), shop_id=service.shop_id, user_id=int(user.id),
            pg_user_id=int(service.pg_user_id), old_token_hash=token_hash(_current_token(service)),
            status=STATUS_PENDING, created_at=now,
        )
        session.add(row)
        await session.flush()
        claimed = OpenReset.of(row)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ValueError(IN_PROGRESS) from None
    except ValueError:
        await session.rollback()
        raise
    return claimed


async def _move(session: AsyncSession, row: OpenReset, *, expected: str, status: str, now: datetime) -> bool:
    """Atomically move the change from ``expected`` to ``status``; False when another worker moved it first."""
    values: dict[str, Any] = {"status": status, "checked_at": now, "attempts": ServiceLinkReset.attempts + 1}
    if status in (STATUS_DONE, STATUS_FAILED):
        values["finished_at"] = now
    result = await session.execute(
        update(ServiceLinkReset).where(ServiceLinkReset.id == row.id, ServiceLinkReset.status == expected)
        .values(**values).execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def _finish(session: AsyncSession, row: OpenReset, link: PanelLink, *, expected: str, now: datetime) -> bool:
    """Save the new link on the service and mark the change done, in one commit."""
    if not await _move(session, row, expected=expected, status=STATUS_DONE, now=now):
        await session.rollback()
        return False
    await save_links(session, {row.pg_user_id: link}, scope=LinkScope(shop_id=row.shop_id))
    await session.commit()
    return True


async def _settle(session: AsyncSession, row: OpenReset, *, expected: str, status: str, now: datetime) -> None:
    if await _move(session, row, expected=expected, status=status, now=now):
        await session.commit()
    else:
        await session.rollback()


def _is_refusal(exc: BaseException) -> bool:
    code = getattr(exc, "status_code", None) if isinstance(exc, PasarGuardError) else None
    return isinstance(code, int) and 400 <= code < 500 and code not in UNSURE_HTTP_STATUSES


async def _revoke(session: AsyncSession, row: OpenReset, pg: Any, now: datetime) -> LinkResetOutcome:
    try:
        answer = await asyncio.wait_for(pg.revoke_sub_by_id(row.pg_user_id), timeout=PANEL_TIMEOUT_SECONDS)
    except (PasarGuardError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        if _is_refusal(exc):
            logger.warning("panel refused link reset service=%s status=%s", row.service_id, exc.status_code)
            await _settle(session, row, expected=STATUS_PENDING, status=STATUS_FAILED, now=now)
            raise ValueError(REFUSED) from None
        logger.warning("link reset answer lost service=%s; reading the panel", row.service_id, exc_info=True)
        await _settle(session, row, expected=STATUS_PENDING, status=STATUS_UNKNOWN, now=now)
        status = await resolve_open_reset(session, replace(row, status=STATUS_UNKNOWN), pg, now=_now())
        return LinkResetOutcome(status, row.service_id)
    link = await link_after_revoke(pg, row.pg_user_id, answer)
    if link is not None and await _finish(session, row, link, expected=STATUS_PENDING, now=now):
        return LinkResetOutcome(STATUS_DONE, row.service_id, link)
    await _settle(session, row, expected=STATUS_PENDING, status=STATUS_UNKNOWN, now=now)
    return LinkResetOutcome(STATUS_UNKNOWN, row.service_id)


async def start_link_reset(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None, ui: Mapping[str, Any]
) -> LinkResetOutcome:
    """Change the service's subscription link; ``ValueError`` carries a message for the customer."""
    now = _now()
    row = await _claim(session, user, service_id, shop_id=shop_id, policy=link_reset_policy(ui), now=now)
    try:
        pg = await _panel(session, row.shop_id)
        # Ends the read of the shop's panel account so no transaction stays open across the panel call.
        await session.commit()
    except (PasarGuardError, httpx.HTTPError, ValueError):
        # No request was sent: the change simply did not start.
        logger.warning("no panel client for link reset service=%s", row.service_id, exc_info=True)
        await _settle(session, row, expected=STATUS_PENDING, status=STATUS_FAILED, now=now)
        raise ValueError(PANEL_DOWN) from None
    return await _revoke(session, row, pg, now)


async def resolve_open_reset(session: AsyncSession, row: OpenReset, pg: Any, *, now: datetime) -> str:
    """Find out what the panel did with an open change by reading it; never revokes. Returns the status."""
    expected = row.status
    try:
        link = await read_panel_link(pg, row.pg_user_id)
    except PANEL_READ_ERRORS:
        logger.warning("link reset check failed service=%s; retried later", row.service_id, exc_info=True)
        await _settle(session, row, expected=expected, status=expected, now=now)
        return expected
    if link is not None and token_hash(link.token) != row.old_token_hash:
        return STATUS_DONE if await _finish(session, row, link, expected=expected, now=now) else expected
    if now - row.created_at >= SETTLE_AFTER:
        await _settle(session, row, expected=expected, status=STATUS_FAILED, now=now)
        return STATUS_FAILED
    await _settle(session, row, expected=expected, status=expected, now=now)
    return expected


async def _due_resets(session: AsyncSession, now: datetime) -> list[OpenReset]:
    """Unknown changes, and pending ones whose request ended long ago (a crash after the claim)."""
    rows = await session.scalars(
        select(ServiceLinkReset).where(
            ServiceLinkReset.status.in_(OPEN_STATUSES),
            or_(ServiceLinkReset.status == STATUS_UNKNOWN, ServiceLinkReset.created_at < now - SETTLE_AFTER),
        ).order_by(ServiceLinkReset.id).limit(JOB_BATCH)
    )
    return [OpenReset.of(row) for row in rows]


async def process_open_link_resets(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Resolve one batch of open changes; one bad row is logged and never stops the batch."""
    now = now or _now()
    rows = await _due_resets(session, now)
    clients: dict[int | None, Any] = {}
    done = 0
    for row in rows:
        try:
            if row.shop_id not in clients:
                clients[row.shop_id] = await _panel(session, row.shop_id)
            await session.commit()
            done += int(await resolve_open_reset(session, row, clients[row.shop_id], now=now) == STATUS_DONE)
        except Exception:  # noqa: BLE001 - one bad row must not stop the batch; the change stays open
            logger.exception("link reset check crashed reset=%s", row.id)
            await session.rollback()
    return done
