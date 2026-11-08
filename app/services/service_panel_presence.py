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
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelAlertState, UserService
from app.services.bot_user_admin import panel_client_for_service, probe_panel_presence
from app.services.panel_alert_notifications import ALERT_PANEL_MISSING, KEY_PANEL_MISSING, Signal

logger = logging.getLogger(__name__)

PRESENT = "present"
ABSENT = "absent"
SCAN_BATCH_LIMIT = 100
SCAN_CONCURRENCY = 4
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


async def missing_signals(session: AsyncSession) -> list[Signal]:
    """One alert per shop (and the platform) that has services the panel no longer knows."""
    rows = (
        await session.execute(
            select(UserService.shop_id, func.count(UserService.id))
            .where(UserService.panel_missing_at.is_not(None), UserService.is_cancelled.is_(False))
            .group_by(UserService.shop_id)
        )
    ).all()
    counts = {(int(shop) if shop else None): int(n) for shop, n in rows}
    known = (
        await session.execute(
            select(PanelAlertState.scope_key).where(
                PanelAlertState.alert == ALERT_PANEL_MISSING, PanelAlertState.active.is_(True)
            )
        )
    ).scalars().all()
    for key in known:
        counts.setdefault(None if key == "platform" else int(key.split(":", 1)[1]), 0)
    return [
        Signal(
            shop_id=owner,
            alert=ALERT_PANEL_MISSING,
            notify_key=KEY_PANEL_MISSING,
            count=count,
            text=missing_text(count),
            path="/users",
        )
        for owner, count in counts.items()
    ]


def missing_text(count: int) -> str:
    return (
        f"🗑 <b>{int(count)}</b> سرویس از پاسارگارد حذف شده است.\n"
        "مشتری‌ها پیام «حذف‌شده از پنل» می‌بینند؛ هر مورد را بررسی و در صورت نیاز بازپرداخت کنید."
    )
