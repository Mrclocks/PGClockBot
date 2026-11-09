"""Delete the services whose cleanup deadline passed, with every safety check repeated at the point of deletion.

A service is deleted only when all of this holds *now*:
* its scope's rule is on and the customer already got the notice (stage 1 or later);
* the panel, read again just now through the shop's own client, still says it is expired / out of volume
  and the deadline (later of the start and the switch-on, plus the days, or a staff hold) has passed;
* it is not exempt, held, cancelled, waiting for a cancellation decision, under shop review, or covered by
  automatic renewal, and no order for it is open or being paid.

Order of effects: the lease is committed first, then the panel user is deleted (a panel user that can only be
disabled is a failure that keeps the row), then the local row, the staff log record and the customer notice. A
panel error leaves the lease in place and the row untouched; the next run retries the same service after the
lease expires, and deleting an already deleted panel user is a clean success, so a repeat is safe.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, ServiceAutomation, UserService
from app.services.bot_user_admin import (
    VOLUME_FINISHED_STATUS,
    admin_delete_service,
    ensure_pg_user_removed_or_absent,
    panel_client_for_service,
    probe_panel_presence,
)
from app.services.customer_notice_queue import add_notice
from app.services.formatting import parse_expire
from app.services.pasarguard import PasarGuardError, is_pg_not_found
from app.services.service_cleanup import (
    LEASE,
    KIND_TIME,
    KIND_VOLUME,
    STAGE_NOTICE,
    CleanupPolicy,
    deadline_for,
    eligible_clauses,
    not_held_clause,
    shop_services_clause,
    time_due_clause,
    volume_due_clause,
)
from app.services.service_cleanup_notices import is_over, live_refresh, stage_column, stage_of
from app.services.staff_audit import ACTOR_SYSTEM, StaffActor, add_staff_action

logger = logging.getLogger(__name__)

DELETE_BATCH = 25
GONE_CONCURRENCY = 4
READ_TIMEOUT_SECONDS = 12
# The customer's last warning must be at least this old, so a warning and the deletion are never one run apart.
MIN_WARNING_GAP = timedelta(hours=6)
SYSTEM_LABEL = "سیستم · پاکسازی خودکار"
OPEN_ORDER_STATUSES = (
    OrderStatus.PENDING.value,
    OrderStatus.AWAITING_RECEIPT.value,
    OrderStatus.AWAITING_APPROVAL.value,
    OrderStatus.PAID.value,
    OrderStatus.DELIVERING.value,
)
DELETED_ACTION = {KIND_TIME: "cleanup_deleted", KIND_VOLUME: "cleanup_volume_deleted"}


@dataclass(frozen=True, slots=True)
class DeleteResult:
    deleted: int = 0
    skipped: int = 0
    failed: int = 0


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _reason(kind: str, days: int) -> str:
    what = "پس از انقضا" if kind == KIND_TIME else "پس از تمام شدن حجم"
    return f"حذف خودکار {days} روز {what} بدون تمدید"[:255]


async def _blocked_by_other_state(session: AsyncSession, service_ids: list[int]) -> set[int]:
    """Services that must not be deleted now: an open order, or automatic renewal switched on."""
    if not service_ids:
        return set()
    open_orders = await session.scalars(
        select(Order.service_id).where(Order.service_id.in_(service_ids), Order.status.in_(OPEN_ORDER_STATUSES))
    )
    auto_renew = await session.scalars(
        select(ServiceAutomation.service_id).where(
            ServiceAutomation.service_id.in_(service_ids), ServiceAutomation.renew_enabled.is_(True)
        )
    )
    return {int(i) for i in open_orders.all() if i} | {int(i) for i in auto_renew.all() if i}


def _due_clause(kind: str, policy: CleanupPolicy, now: datetime):
    make = time_due_clause if kind == KIND_TIME else volume_due_clause
    return make(policy, now)


async def _claim_lease(
    session: AsyncSession, service_id: int, *, kind: str, now: datetime
) -> bool:
    """Take the lease only if the service is *still* due: every condition is part of the UPDATE itself.

    Staff may exempt a service or give it more days while a batch is running; a decision made before
    that must not delete it, so the check and the claim are one atomic statement.
    """
    over = (
        and_(UserService.quota_expire_at.is_not(None), UserService.quota_expire_at < now)
        if kind == KIND_TIME
        else UserService.volume_exhausted_at.is_not(None)
    )
    result = await session.execute(
        update(UserService)
        .where(
            UserService.id == service_id,
            *eligible_clauses(),
            not_held_clause(now),
            over,
            stage_column(kind) >= STAGE_NOTICE,
            UserService.cleanup_warned_at.is_not(None),
            UserService.cleanup_warned_at <= now - MIN_WARNING_GAP,
            or_(UserService.cleanup_claimed_at.is_(None), UserService.cleanup_claimed_at < now - LEASE),
        )
        .values(cleanup_claimed_at=now),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    return result.rowcount == 1


async def delete_due_services(
    session: AsyncSession, *, shop_id: int | None, policy: CleanupPolicy, kind: str, now: datetime | None = None,
    limit: int = DELETE_BATCH,
) -> DeleteResult:
    """Delete up to ``limit`` services of one scope and rule that are due; returns what happened."""
    now = now or datetime.now(timezone.utc)
    due = _due_clause(kind, policy, now)
    if due is None:
        return DeleteResult()
    column = stage_column(kind)
    days = policy.time_days if kind == KIND_TIME else policy.volume_days
    rows = (
        await session.execute(
            select(UserService, BotUser.telegram_id)
            .join(BotUser, BotUser.id == UserService.bot_user_id)
            .where(
                shop_services_clause(shop_id), *eligible_clauses(), not_held_clause(now), due,
                column >= STAGE_NOTICE, UserService.cleanup_warned_at <= now - MIN_WARNING_GAP, BotUser.is_blocked.is_(False),
                or_(UserService.cleanup_claimed_at.is_(None), UserService.cleanup_claimed_at < now - LEASE),
            )
            .order_by(UserService.id)
            .limit(limit)
        )
    ).all()
    if not rows:
        return DeleteResult()
    services = [service for service, _tg in rows]
    telegram = {int(service.id): int(tg or 0) for service, tg in rows}
    blocked = await _blocked_by_other_state(session, [int(s.id) for s in services])
    fresh = await live_refresh(session, services)
    # A service the panel could not be read for may simply have no panel user any more (an earlier run deleted it
    # there and then failed locally): confirm that with the shop's own client, so the leftover row is cleaned up.
    gone = await _confirmed_gone(session, [s for s in services if int(s.id) not in fresh])
    await session.commit()
    # Decide for every service first, from the freshly read data: a failed delete rolls the session back,
    # which would expire the other loaded rows.
    decisions: list[tuple[int, int]] = []
    skipped = 0
    for service in services:
        service_id = int(service.id)
        deadline = deadline_for(service, policy, kind)
        still_due = (
            (service_id in fresh or service_id in gone) and service_id not in blocked and is_over(service, kind, now)
            and deadline is not None and deadline <= now and stage_of(service, kind) >= STAGE_NOTICE
            and not service.cleanup_exempt and service.cleanup_warned_at is not None
            and _aware(service.cleanup_warned_at) <= now - MIN_WARNING_GAP
        )
        if still_due:
            decisions.append((service_id, telegram[service_id]))
        else:
            skipped += 1
    deleted = failed = 0
    for service_id, telegram_id in decisions:
        # Open orders and automatic renewal are read again right before this service, not once per batch.
        if await _blocked_by_other_state(session, [service_id]) or not await _claim_lease(session, service_id, kind=kind, now=now):
            skipped += 1
            continue
        service = await session.get(UserService, service_id)
        if service is None:
            continue
        outcome = await _delete_one(session, service, kind=kind, days=days, telegram_id=telegram_id, shop_id=shop_id, now=now)
        deleted += outcome == "deleted"
        failed += outcome == "failed"
        skipped += outcome == "skipped"
    return DeleteResult(deleted=deleted, skipped=skipped, failed=failed)


async def _read_panel_user(pg, pg_user_id: int) -> dict | None:
    """The panel user record, ``None`` when the panel says it does not exist; any other failure raises."""
    try:
        record = await asyncio.wait_for(pg.get_user_by_id(int(pg_user_id)), timeout=READ_TIMEOUT_SECONDS)
    except PasarGuardError as exc:
        if is_pg_not_found(exc):
            return None
        raise
    return record if isinstance(record, dict) else None


async def _confirmed_gone(session: AsyncSession, services: list[UserService]) -> set[int]:
    """Services whose panel user is confirmed missing: a clean 404 from the shop's own client, which can read its own admin."""
    if not services:
        return set()
    clients = [await panel_client_for_service(session, service) for service in services]
    gate = asyncio.Semaphore(GONE_CONCURRENCY)

    async def probe(pg, service: UserService) -> bool:
        async with gate:
            return await probe_panel_presence(pg, service.pg_user_id) == "absent"

    answers = await asyncio.gather(*(probe(pg, s) for pg, s in zip(clients, services, strict=True)))
    return {int(s.id) for s, gone in zip(services, answers, strict=True) if gone}


def _still_over(info: dict, kind: str, now: datetime) -> bool:
    """Whether a panel user record still shows the service as expired / out of volume."""
    if kind == KIND_TIME:
        expire = parse_expire(info.get("expire"))
        return expire is not None and (expire if expire.tzinfo else expire.replace(tzinfo=timezone.utc)) < now
    return str(info.get("status") or "").strip().lower() == VOLUME_FINISHED_STATUS


async def _release_lease(session: AsyncSession, service_id: int) -> None:
    await session.execute(
        update(UserService).where(UserService.id == service_id).values(cleanup_claimed_at=None),
        execution_options={"synchronize_session": False},
    )
    await session.commit()


async def _delete_one(
    session: AsyncSession, service: UserService, *, kind: str, days: int, telegram_id: int, shop_id: int | None,
    now: datetime,
) -> str:
    """Delete one claimed service; on any failure the row and the lease stay as they were."""
    service_id, user_id = int(service.id), int(service.bot_user_id)
    name, owner_shop = service.pg_username or "", service.shop_id
    pg_user_id = int(service.pg_user_id) if service.pg_user_id else None
    actor = StaffActor(shop_id=shop_id, kind=ACTOR_SYSTEM, user_id=None, label=SYSTEM_LABEL)
    try:
        pg = await panel_client_for_service(session, service)
        # End the read transaction first: no database transaction is open while the panel is called.
        await session.commit()
        if pg is None or pg_user_id is None:
            raise ValueError("panel client unavailable")
        # One last read right before the irreversible step: a renewal made in the panel meanwhile stops it.
        # A clean 404 means the panel user is already gone and only the local row is left to remove.
        record = await _read_panel_user(pg, pg_user_id)
        if record is not None and not _still_over(record, kind, now):
            await _release_lease(session, service_id)
            return "skipped"
        await ensure_pg_user_removed_or_absent(pg, pg_user_id, staff=None, allow_disable_fallback=False)
        service = await session.get(UserService, service_id)
        if service is not None:
            # The log record joins the local delete's own commit, so a deleted service always has its record.
            add_staff_action(
                session, actor, action="service_auto_delete", target_type="service", target_id=service_id,
                reason=_reason(kind, days),
            )
            await admin_delete_service(session, service, delete_pg=False, staff=None)
    except Exception:  # noqa: BLE001 - unknown panel outcome: keep the row and the lease, retry after the lease expires
        logger.warning("service cleanup delete failed service=%s", service_id, exc_info=True)
        await session.rollback()
        return "failed"
    if telegram_id:
        try:
            add_notice(
                session, shop_id=owner_shop, bot_user_id=user_id, telegram_id=telegram_id,
                action=DELETED_ACTION[kind], service_name=name, reason="",
            )
            await session.commit()
        except Exception:  # noqa: BLE001 - the service is gone; only the customer message is lost
            logger.warning("service cleanup notice failed service=%s", service_id, exc_info=True)
            await session.rollback()
    return "deleted"
