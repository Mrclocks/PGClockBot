"""Which customer services the panel no longer knows, and what to tell people about them.

A service is marked missing only after the shop's own panel client got a clean HTTP 404 on the
user lookup. A timeout, a 5xx or a refusal changes nothing, so an outage never hides services.
The mark is a flag on the row: the service, its orders and its wallet history stay as they were,
and nothing is refunded or deleted from here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelAlertState, UserService
from app.services.bot_user_admin import panel_client_for_service, probe_panel_presence
from app.services.panel_alert_notifications import (
    ALERT_PANEL_MISSING,
    KEY_PANEL_MISSING,
    PLATFORM_SCOPE,
    Signal,
    scope_key,
)

logger = logging.getLogger(__name__)

PRESENT = "present"
ABSENT = "absent"
SCAN_BATCH_LIMIT = 100
SCAN_CONCURRENCY = 4
# The scan revisits every service several times a day; staff hear about new removals once a day at most.
MISSING_ALERT_INTERVAL = timedelta(hours=24)
STATUS_LABEL = "حذف‌شده از پنل"
CUSTOMER_TEXT = (
    "این سرویس دیگر روی سرور فعال نیست و از پنل حذف شده است.\n"
    "برای بررسی، بازگرداندن یا بازپرداخت با پشتیبانی تماس بگیرید یا درخواست لغو ثبت کنید."
)


@dataclass(frozen=True, slots=True)
class PresenceResult:
    service_id: int
    presence: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def record_presence(session: AsyncSession, service_id: int, presence: str, *, now: datetime | None = None) -> bool:
    """Store one lookup result; True when the missing flag changed.

    Both directions are conditional updates, so two workers cannot flip the flag back and forth
    and an ``unknown`` result leaves the flag exactly as it was.
    """
    now = now or _utcnow()
    condition = UserService.id == int(service_id)
    flag_change = {
        ABSENT: (UserService.panel_missing_at.is_(None), now),
        PRESENT: (UserService.panel_missing_at.is_not(None), None),
    }.get(presence)
    changed = False
    if flag_change is not None:
        needs_change, flag_value = flag_change
        result = await session.execute(
            update(UserService)
            .where(condition, needs_change)
            .values(panel_missing_at=flag_value, panel_checked_at=now),
            execution_options={"synchronize_session": False},
        )
        changed = result.rowcount == 1
    if not changed:
        # Every lookup, including an inconclusive one, moves the service to the back of the
        # queue so services that never answer cannot starve the rest of the scan.
        await session.execute(
            update(UserService).where(condition).values(panel_checked_at=now),
            execution_options={"synchronize_session": False},
        )
    await session.commit()
    return changed


async def refresh_service_presence(session: AsyncSession, service: UserService) -> str:
    """Look one service up now (used when a customer opens a card the panel could not answer)."""
    pg = await panel_client_for_service(session, service)
    presence = await probe_panel_presence(pg, service.pg_user_id)
    await record_presence(session, int(service.id), presence)
    await session.refresh(service)
    return presence


async def _due_services(session: AsyncSession, limit: int) -> list[UserService]:
    """Active services with a panel user, least recently checked first (never-checked first)."""
    return list(
        (
            await session.scalars(
                select(UserService)
                .where(
                    UserService.pg_user_id.is_not(None),
                    UserService.is_cancelled.is_(False),
                    UserService.shop_needs_review.is_(False),
                )
                .order_by(UserService.panel_checked_at.asc().nulls_first(), UserService.id)
                .limit(limit)
            )
        ).all()
    )


async def scan_panel_presence(session: AsyncSession, *, limit: int = SCAN_BATCH_LIMIT) -> list[PresenceResult]:
    """Check one bounded batch; one failing service is logged and skipped."""
    services = await _due_services(session, limit)
    # One session cannot run queries concurrently, so every shop client is resolved first and
    # only the panel calls overlap.
    clients = [await panel_client_for_service(session, service) for service in services]
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def probe(service: UserService, pg: object | None) -> PresenceResult:
        async with gate:
            return PresenceResult(int(service.id), await probe_panel_presence(pg, service.pg_user_id))

    results: list[PresenceResult] = []
    outcomes = await asyncio.gather(
        *(probe(s, pg) for s, pg in zip(services, clients, strict=True)), return_exceptions=True
    )
    for item in outcomes:
        if isinstance(item, BaseException):
            logger.warning("panel presence probe failed", exc_info=item)
            continue
        results.append(item)
    for result in results:
        await record_presence(session, result.service_id, result.presence)
    return results


def _removed_while_usable() -> ColumnElement[bool]:
    """Marked services that were still usable when they disappeared, by their last cached quota.

    The panel deletes expired and volume-finished users on its own schedule; those are routine, owe
    the customer nothing, and are left to the expired-service cleanup instead of the staff alert.
    """
    return and_(
        UserService.panel_missing_at.is_not(None),
        UserService.is_cancelled.is_(False),
        UserService.volume_exhausted_at.is_(None),
        or_(UserService.quota_expire_at.is_(None), UserService.quota_expire_at > UserService.panel_missing_at),
    )


def _aware(moment: datetime | None) -> datetime | None:
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _fingerprint(newest: datetime | None, state: PanelAlertState | None) -> str:
    """Changes only when a service was marked after the last announcement.

    Removing marked services (the newest included) keeps the previous value, so a cleanup never
    triggers a new message on its own.
    """
    newest = _aware(newest)
    notified_at = _aware(state.notified_at) if state is not None else None
    if state is not None and notified_at is not None and (newest is None or newest <= notified_at):
        return state.fingerprint
    return newest.isoformat() if newest is not None else ""


async def missing_signals(session: AsyncSession) -> list[Signal]:
    """One alert per shop (and the platform) with services removed from the panel while still usable.

    It is announced when such services first appear, and again only for services marked after
    the previous announcement, at most once per ``MISSING_ALERT_INTERVAL``.
    """
    rows = (
        await session.execute(
            select(UserService.shop_id, func.count(UserService.id), func.max(UserService.panel_missing_at))
            .where(_removed_while_usable())
            .group_by(UserService.shop_id)
        )
    ).all()
    found = {(int(shop) if shop else None): (int(n), newest) for shop, n, newest in rows}
    states = {
        state.scope_key: state
        for state in (
            await session.scalars(select(PanelAlertState).where(PanelAlertState.alert == ALERT_PANEL_MISSING))
        ).all()
    }
    for key, state in states.items():
        if state.active:
            found.setdefault(None if key == PLATFORM_SCOPE else int(key.split(":", 1)[1]), (0, None))
    return [
        Signal(
            shop_id=owner,
            alert=ALERT_PANEL_MISSING,
            notify_key=KEY_PANEL_MISSING,
            count=count,
            text=missing_text(count),
            path="/users",
            fingerprint=_fingerprint(newest, states.get(scope_key(owner))),
            min_interval=MISSING_ALERT_INTERVAL,
        )
        for owner, (count, newest) in found.items()
    ]


def missing_text(count: int) -> str:
    return (
        f"🗑 <b>{int(count)}</b> سرویس پیش از پایان اعتبار از پاسارگارد حذف شده است.\n"
        "مشتری‌ها پیام «حذف‌شده از پنل» می‌بینند؛ هر مورد را بررسی و در صورت نیاز بازپرداخت کنید.\n"
        "سرویس‌های منقضی یا تمام‌حجم که پنل حذف کرده شمرده نمی‌شوند."
    )
