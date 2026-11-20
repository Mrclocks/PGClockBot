"""A customer pauses a service for a few days and keeps the time that was left.

Freezing disables the user on the panel and saves the seconds left; resuming sets the expiry to
now plus those seconds and enables the user. The customer resumes early, or the job resumes the
service at the deadline. Each step is claimed in the database before the panel call and is safe
to repeat: disabling is idempotent, and the resume writes an absolute expiry saved before the
call, so the job retries a step whose outcome was unknown with the same numbers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, ServiceFreeze, UserService
from app.services.formatting import parse_expire
from app.services.pasarguard import PasarGuardError, get_pg, get_pg_for_reseller
from app.services.service_import import is_external_service
from app.services.service_freeze_settings import (
    BY_CUSTOMER,
    BY_DEADLINE,
    CLEANUP_HOLD_MARGIN,
    JOB_BATCH,
    OPEN_STATUSES,
    PERIOD,
    STATUS_DONE,
    STATUS_FROZEN,
    STATUS_PAUSING,
    STATUS_RESUMING,
    FreezePolicy,
    freeze_policy,
)
from app.services.service_scope import service_shop_id

logger = logging.getLogger(__name__)
PANEL_DOWN = "پنل الان پاسخ نداد؛ چند دقیقهٔ دیگر دوباره امتحان کنید"

def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _panel(session: AsyncSession, shop_id: int | None):
    return await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()


async def open_freeze(session: AsyncSession, service_id: int) -> ServiceFreeze | None:
    return await session.scalar(
        select(ServiceFreeze).where(ServiceFreeze.service_id == int(service_id), ServiceFreeze.status.in_(OPEN_STATUSES))
    )


def can_be_frozen(service: UserService) -> bool:
    """A service this bot sold and still owns on the panel; not cancelled or being cancelled."""
    return bool(
        service.pg_user_id and not is_external_service(service)
        and not service.is_cancelled and not service.cancellation_pending
    )


BUTTON_PAUSE = "pause"
BUTTON_RESUME = "resume"


async def freeze_button(session: AsyncSession, service: UserService, ui: Mapping[str, Any]) -> str | None:
    """Which freeze button the service's «⚙️ بیشتر» shows, if any."""
    if not can_be_frozen(service):
        return None
    row = await open_freeze(session, int(service.id))
    if row is not None:
        return BUTTON_RESUME if row.status == STATUS_FROZEN else None
    return BUTTON_PAUSE if freeze_policy(ui).enabled else None


async def _owned_service(session: AsyncSession, user: BotUser, service_id: int, shop_id: int | None) -> UserService:
    service = await session.get(UserService, int(service_id))
    if service is None or int(service.bot_user_id) != int(user.id) or user.is_blocked:
        raise ValueError("سرویس یافت نشد")
    if await service_shop_id(session, service) != shop_id:
        raise ValueError("سرویس یافت نشد")
    if not can_be_frozen(service):
        raise ValueError("این سرویس را نمی‌توان متوقف کرد")
    return service


async def _freezes_in_period(session: AsyncSession, service: UserService) -> int:
    since = _now() - PERIOD
    used = await session.scalar(
        select(func.count(ServiceFreeze.id)).where(
            ServiceFreeze.service_id == int(service.id), ServiceFreeze.created_at >= since
        )
    )
    return int(used or 0)


async def _has_paid_mutation(session: AsyncSession, service_id: int) -> bool:
    found = await session.scalar(
        select(Order.id).where(
            Order.service_id == int(service_id),
            Order.status.in_([OrderStatus.PAID.value, OrderStatus.DELIVERING.value]),
        ).limit(1)
    )
    return found is not None


def _seconds_left(info: Mapping[str, Any], now: datetime, policy: FreezePolicy) -> int:
    """Time left on an active service, or a ValueError the customer can read."""
    if str(info.get("status") or "").lower() != "active":
        raise ValueError("فقط سرویس فعال را می‌توان متوقف کرد")
    expire = parse_expire(info.get("expire") or info.get("expire_date"))
    if expire is None:
        raise ValueError("این سرویس زمان نامحدود دارد و نیازی به توقف ندارد")
    left = int((expire - now).total_seconds())
    if left <= max(policy.min_days_left * 86400, 3600):
        raise ValueError("زمان باقی‌ماندهٔ این سرویس برای توقف کافی نیست")
    limit, used = int(info.get("data_limit") or 0), int(info.get("used_traffic") or 0)
    if limit > 0 and used >= limit:
        raise ValueError("حجم این سرویس تمام شده است")
    return left


async def _claim_freeze(
    session: AsyncSession, service: UserService, user: BotUser, *, shop_id: int | None, left: int, days: int, now: datetime
) -> ServiceFreeze:
    resume_by = now + timedelta(days=days)
    row = ServiceFreeze(
        service_id=int(service.id), shop_id=shop_id, user_id=int(user.id), status=STATUS_PAUSING,
        remaining_seconds=left, resume_by=resume_by,
    )
    hold = resume_by + CLEANUP_HOLD_MARGIN
    if service.cleanup_hold_until is None or service.cleanup_hold_until < hold:
        service.cleanup_hold_until = hold
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        raise ValueError("این سرویس همین حالا متوقف است") from None
    await session.commit()
    return row


async def _disable(session: AsyncSession, row: ServiceFreeze, service: UserService) -> None:
    """Disable on the panel; on success the freeze is ``frozen``, else the job retries."""
    pg = await _panel(session, row.shop_id)
    await pg.set_disabled_by_id(int(service.pg_user_id), True)
    result = await session.execute(
        update(ServiceFreeze).where(ServiceFreeze.id == int(row.id), ServiceFreeze.status == STATUS_PAUSING)
        .values(status=STATUS_FROZEN).execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount == 1:
        row.status = STATUS_FROZEN


async def start_freeze(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None, days: int, ui: Mapping[str, Any]
) -> ServiceFreeze:
    """Pause the customer's service for ``days``; raises ValueError with a message to show."""
    policy = freeze_policy(ui)
    if not policy.enabled:
        raise ValueError("توقف موقت سرویس در این فروشگاه فعال نیست")
    if isinstance(days, bool) or not 1 <= int(days) <= policy.max_days:
        raise ValueError(f"مدت توقف باید بین ۱ و {policy.max_days} روز باشد")
    service = await _owned_service(session, user, service_id, shop_id)
    if await _freezes_in_period(session, service) >= policy.per_period:
        raise ValueError("سقف توقف این سرویس در ۳۰ روز گذشته پر شده است")
    if await _has_paid_mutation(session, int(service.id)):
        raise ValueError("یک سفارش روی این سرویس در حال انجام است؛ کمی بعد تلاش کنید")
    now = _now()
    try:
        info = await (await _panel(session, shop_id)).get_user_by_id(int(service.pg_user_id))
    except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
        raise ValueError(PANEL_DOWN) from exc
    left = _seconds_left(info if isinstance(info, dict) else {}, now, policy)
    row = await _claim_freeze(session, service, user, shop_id=shop_id, left=left, days=int(days), now=now)
    try:
        await _disable(session, row, service)
    except Exception:  # noqa: BLE001 - the claim is saved; disabling is idempotent and the job retries it
        logger.warning("freeze disable failed freeze=%s; the job retries", row.id, exc_info=True)
        await session.rollback()
        # The rollback expired the row; reload the committed claim so the caller can read it.
        await session.refresh(row)
    return row


async def _claim_resume(session: AsyncSession, row: ServiceFreeze, *, by: str, now: datetime) -> bool:
    target = int(now.timestamp()) + int(row.remaining_seconds)
    result = await session.execute(
        update(ServiceFreeze).where(ServiceFreeze.id == int(row.id), ServiceFreeze.status == STATUS_FROZEN)
        .values(status=STATUS_RESUMING, target_expire=target, resumed_by=by)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount != 1:
        return False
    row.status, row.target_expire, row.resumed_by = STATUS_RESUMING, target, by
    return True


async def _apply_resume(session: AsyncSession, row: ServiceFreeze) -> bool:
    """Write the saved expiry and enable the user; True once the freeze is done."""
    service = await session.get(UserService, int(row.service_id))
    if service is None or not service.pg_user_id:
        return False
    pg = await _panel(session, row.shop_id)
    await pg.modify_user_by_id(int(service.pg_user_id), {"expire": int(row.target_expire), "status": "active"})
    result = await session.execute(
        update(ServiceFreeze).where(ServiceFreeze.id == int(row.id), ServiceFreeze.status == STATUS_RESUMING)
        .values(status=STATUS_DONE, resumed_at=_now()).execution_options(synchronize_session=False)
    )
    service.quota_expire_at = datetime.fromtimestamp(int(row.target_expire), tz=timezone.utc)
    await session.commit()
    return result.rowcount == 1


async def resume_freeze(session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None) -> ServiceFreeze:
    """The customer ends the pause early; raises ValueError with a message to show."""
    service = await session.get(UserService, int(service_id))
    if service is None or int(service.bot_user_id) != int(user.id) or await service_shop_id(session, service) != shop_id:
        raise ValueError("سرویس یافت نشد")
    row = await open_freeze(session, int(service.id))
    if row is None or row.status != STATUS_FROZEN:
        raise ValueError("این سرویس متوقف نیست یا در حال اعمال تغییر است")
    if not await _claim_resume(session, row, by=BY_CUSTOMER, now=_now()):
        raise ValueError("این سرویس در حال فعال شدن است")
    try:
        if await _apply_resume(session, row):
            row.status = STATUS_DONE
    except Exception:  # noqa: BLE001 - the target is saved; the job writes the same expiry again
        logger.warning("freeze resume failed freeze=%s; the job retries", row.id, exc_info=True)
        await session.rollback()
        # The rollback expired the row; reload the committed claim so the caller can read it.
        await session.refresh(row)
    return row


async def _due(session: AsyncSession, now: datetime) -> list[ServiceFreeze]:
    rows = await session.scalars(
        select(ServiceFreeze).where(
            (ServiceFreeze.status.in_([STATUS_PAUSING, STATUS_RESUMING]))
            | ((ServiceFreeze.status == STATUS_FROZEN) & (ServiceFreeze.resume_by <= now))
        ).order_by(ServiceFreeze.id).limit(JOB_BATCH)
    )
    return list(rows)


async def _advance(session: AsyncSession, row: ServiceFreeze, now: datetime) -> str | None:
    """One step of a due freeze; returns ``deadline`` when it was resumed at the deadline."""
    if row.status == STATUS_PAUSING:
        service = await session.get(UserService, int(row.service_id))
        if service is not None and service.pg_user_id:
            await _disable(session, row, service)
        return None
    if row.status == STATUS_FROZEN and not await _claim_resume(session, row, by=BY_DEADLINE, now=now):
        return None
    done = await _apply_resume(session, row)
    return row.resumed_by if done else None


async def process_due_freezes(session: AsyncSession, *, now: datetime | None = None) -> list[ServiceFreeze]:
    """Retry unfinished steps and resume freezes past their deadline; one bad row never stops the batch."""
    now = now or _now()
    resumed: list[ServiceFreeze] = []
    for row in await _due(session, now):
        try:
            if await _advance(session, row, now) == BY_DEADLINE:
                resumed.append(row)
        except Exception:  # noqa: BLE001 - per-row isolation in a background job; the row is retried
            logger.warning("freeze step failed freeze=%s status=%s", row.id, row.status, exc_info=True)
            await session.rollback()
    return resumed
