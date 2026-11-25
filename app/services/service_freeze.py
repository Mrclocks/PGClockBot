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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, Plan, ServiceFreeze, UserService
from app.services.account_hold_guard import defer_reenable_to_holds
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
    STATUS_CLOSED,
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


async def is_on_paid_plan(session: AsyncSession, service: UserService) -> bool:
    """Only services bought on a sellable plan; a trial or a service with no plan never qualifies.

    «همهٔ پلن‌ها» means every paid plan: a free trial frozen or handed on would turn free test
    accounts into extra paid time.
    """
    if service.plan_id is None:
        return False
    is_trial = await session.scalar(select(Plan.is_trial).where(Plan.id == int(service.plan_id)))
    return is_trial is False


async def freeze_button(session: AsyncSession, service: UserService, ui: Mapping[str, Any]) -> str | None:
    """Which freeze button the service's «⚙️ بیشتر» shows, if any."""
    if not can_be_frozen(service):
        return None
    row = await open_freeze(session, int(service.id))
    if row is not None:
        return BUTTON_RESUME if row.status == STATUS_FROZEN else None
    if not freeze_policy(ui).allows_plan(service.plan_id):
        return None
    return BUTTON_PAUSE if await is_on_paid_plan(session, service) else None


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


# An order still in flight would apply to the service after the freeze computed its time.
OPEN_ORDER_STATUSES = (
    OrderStatus.PENDING.value, OrderStatus.AWAITING_RECEIPT.value, OrderStatus.AWAITING_APPROVAL.value,
    OrderStatus.PAID.value, OrderStatus.DELIVERING.value,
)


async def has_open_order(session: AsyncSession, service_id: int) -> bool:
    found = await session.scalar(
        select(Order.id).where(Order.service_id == int(service_id), Order.status.in_(OPEN_ORDER_STATUSES)).limit(1)
    )
    return found is not None


@dataclass(frozen=True, slots=True)
class PanelTime:
    """The panel's expiry (unix seconds, None = unlimited) and the seconds left at ``now``."""

    expire_ts: int | None
    left: int


def _seconds_left(info: Mapping[str, Any], now: datetime, policy: FreezePolicy) -> PanelTime:
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
    return PanelTime(int(expire.timestamp()), left)


async def _claim_freeze(
    session: AsyncSession, service: UserService, user: BotUser, *, shop_id: int | None, time_left: PanelTime,
    days: int, now: datetime,
) -> ServiceFreeze:
    resume_by = now + timedelta(days=days)
    row = ServiceFreeze(
        service_id=int(service.id), shop_id=shop_id, user_id=int(user.id), status=STATUS_PAUSING,
        remaining_seconds=time_left.left, frozen_expire=time_left.expire_ts, resume_by=resume_by,
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


def _left_after_delay(row: ServiceFreeze, now: datetime) -> int:
    """Seconds left once disabled: a disable that came late does not give back the time used meanwhile."""
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    used = max(0, int((now - created).total_seconds()))
    return max(0, int(row.remaining_seconds) - used)


async def _disable(session: AsyncSession, row: ServiceFreeze, service: UserService) -> None:
    """Disable on the panel; on success the freeze is ``frozen``, else the job retries."""
    pg = await _panel(session, row.shop_id)
    await pg.set_disabled_by_id(int(service.pg_user_id), True)
    left = _left_after_delay(row, _now())
    result = await session.execute(
        update(ServiceFreeze).where(ServiceFreeze.id == int(row.id), ServiceFreeze.status == STATUS_PAUSING)
        .values(status=STATUS_FROZEN, remaining_seconds=left).execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount == 1:
        row.status, row.remaining_seconds = STATUS_FROZEN, left


async def _automation_lease(session: AsyncSession, service: UserService) -> str:
    """Hold the service's automation lease so no automatic purchase runs while the freeze starts."""
    # Deferred: service_automation imports the order services, which import the freeze guard.
    from app.services.service_automation import claim_automation, get_automation

    await get_automation(session, service)
    await session.commit()
    token = await claim_automation(session, int(service.id))
    if token is None:
        raise ValueError("یک کار خودکار روی این سرویس در حال انجام است؛ کمی بعد تلاش کنید")
    return token


async def _read_time_left(session: AsyncSession, service: UserService, shop_id: int | None, now: datetime,
                          policy: FreezePolicy) -> PanelTime:
    try:
        info = await (await _panel(session, shop_id)).get_user_by_id(int(service.pg_user_id))
    except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
        raise ValueError(PANEL_DOWN) from exc
    return _seconds_left(info if isinstance(info, dict) else {}, now, policy)


async def _validate_start(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None, days: int, policy: FreezePolicy
) -> UserService:
    if not policy.enabled:
        raise ValueError("توقف موقت سرویس در این فروشگاه فعال نیست")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= policy.max_days:
        raise ValueError(f"مدت توقف باید بین ۱ و {policy.max_days} روز باشد")
    service = await _owned_service(session, user, service_id, shop_id)
    if not policy.plans.allows(service.plan_id) or not await is_on_paid_plan(session, service):
        raise ValueError("توقف موقت برای پلن این سرویس فعال نیست")
    if await _freezes_in_period(session, service) >= policy.per_period:
        raise ValueError("سقف توقف این سرویس در ۳۰ روز گذشته پر شده است")
    if await has_open_order(session, int(service.id)):
        raise ValueError("یک سفارش روی این سرویس در حال انجام است؛ کمی بعد تلاش کنید")
    return service


async def start_freeze(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None, days: int, ui: Mapping[str, Any]
) -> ServiceFreeze:
    """Pause the customer's service for ``days``; raises ValueError with a message to show."""
    # Deferred like _automation_lease: the lease release lives with the automation service.
    from app.services.service_automation import release_automation

    policy = freeze_policy(ui)
    service = await _validate_start(session, user, service_id, shop_id=shop_id, days=days, policy=policy)
    token = await _automation_lease(session, service)
    try:
        now = _now()
        time_left = await _read_time_left(session, service, shop_id, now, policy)
        row = await _claim_freeze(session, service, user, shop_id=shop_id, time_left=time_left, days=days, now=now)
        try:
            await _disable(session, row, service)
        except Exception:  # noqa: BLE001 - the claim is saved; disabling is idempotent and the job retries it
            logger.warning("freeze disable failed freeze=%s; the job retries", row.id, exc_info=True)
            await session.rollback()
            # The rollback expired the row; reload the committed claim so the caller can read it.
            await session.refresh(row)
        return row
    finally:
        await release_automation(session, int(service.id), token)


def _resume_target(row: ServiceFreeze, info: Mapping[str, Any], now: datetime) -> int:
    """The expiry to write: now plus the time held, plus any change staff made on the panel meanwhile.

    0 means unlimited, only when the panel says so (staff made the service unlimited meanwhile).
    A user something else already enabled has been using its panel time since, so that expiry is
    kept as it is: adding the held time again would count it twice.
    """
    if "expire" not in info and "expire_date" not in info:
        raise ValueError("پاسخ پنل تاریخ انقضا ندارد")
    raw = info.get("expire") if "expire" in info else info.get("expire_date")
    expire = parse_expire(raw)
    if expire is None:
        if raw not in (None, 0, "0", ""):
            raise ValueError("تاریخ انقضای پنل خوانا نیست")
        return 0
    expire_ts = int(expire.timestamp())
    if str(info.get("status") or "").lower() == "active":
        return expire_ts
    staff_change = expire_ts - int(row.frozen_expire) if row.frozen_expire else 0
    return max(int(now.timestamp()), int(now.timestamp()) + int(row.remaining_seconds) + staff_change)


async def _close(session: AsyncSession, row: ServiceFreeze, *, expected: str) -> None:
    """End a freeze whose service is gone or cancelled, without re-enabling anything."""
    await session.execute(
        update(ServiceFreeze).where(ServiceFreeze.id == int(row.id), ServiceFreeze.status == expected)
        .values(status=STATUS_CLOSED, resumed_at=_now()).execution_options(synchronize_session=False)
    )
    await session.commit()
    row.status = STATUS_CLOSED


def _service_is_gone(service: UserService | None) -> bool:
    return service is None or not service.pg_user_id or bool(service.is_cancelled)


async def _stays_off(session: AsyncSession, service: UserService | None) -> bool:
    """The service must not be enabled again: it is gone or cancelled, or its owner is blocked."""
    if _service_is_gone(service):
        return True
    owner = await session.get(BotUser, int(service.bot_user_id))
    return owner is None or bool(owner.is_blocked)


async def _claim_resume(session: AsyncSession, row: ServiceFreeze, *, by: str, now: datetime) -> bool:
    """Save the target expiry before the panel write, so a retry writes the same one."""
    service = await session.get(UserService, int(row.service_id))
    if await _stays_off(session, service):
        await _close(session, row, expected=STATUS_FROZEN)
        return False
    try:
        info = await (await _panel(session, row.shop_id)).get_user_by_id(int(service.pg_user_id))
    except PasarGuardError as exc:
        if exc.status_code == 404:
            await _close(session, row, expected=STATUS_FROZEN)
            return False
        raise
    if not isinstance(info, dict):
        raise ValueError("پاسخ پنل نامعتبر است")
    target = _resume_target(row, info, now)
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
    if await _stays_off(session, service):
        await _close(session, row, expected=STATUS_RESUMING)
        return False
    pg = await _panel(session, row.shop_id)
    payload: dict[str, object] = {"expire": int(row.target_expire), "status": "active"}
    # A held account keeps the user off: write the saved days; the hold's release turns it on.
    if await defer_reenable_to_holds(session, [int(service.pg_user_id)]):
        payload.pop("status")
    await session.commit()
    try:
        await pg.modify_user_by_id(int(service.pg_user_id), payload)
    except PasarGuardError as exc:
        if exc.status_code != 404:
            raise
        await _close(session, row, expected=STATUS_RESUMING)
        return False
    result = await session.execute(
        update(ServiceFreeze).where(ServiceFreeze.id == int(row.id), ServiceFreeze.status == STATUS_RESUMING)
        .values(status=STATUS_DONE, resumed_at=_now()).execution_options(synchronize_session=False)
    )
    target = int(row.target_expire)
    service.quota_expire_at = datetime.fromtimestamp(target, tz=timezone.utc) if target else None
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
    try:
        claimed = await _claim_resume(session, row, by=BY_CUSTOMER, now=_now())
    except (PasarGuardError, httpx.HTTPError, TimeoutError, ValueError) as exc:
        raise ValueError(PANEL_DOWN) from exc
    if not claimed:
        raise ValueError("این سرویس در حال فعال شدن است یا دیگر وجود ندارد")
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
        if _service_is_gone(service):
            await _close(session, row, expected=STATUS_PAUSING)
        else:
            await _disable(session, row, service)
        return None
    if row.status == STATUS_FROZEN and not await _claim_resume(session, row, by=BY_DEADLINE, now=now):
        return None
    done = await _apply_resume(session, row)
    return row.resumed_by if done else None


async def process_due_freezes(session: AsyncSession, *, now: datetime | None = None) -> list[ServiceFreeze]:
    """Retry unfinished steps and resume freezes past their deadline; one bad row never stops the batch."""
    now = now or _now()
    resumed_ids: list[int] = []
    for freeze_id in [int(row.id) for row in await _due(session, now)]:
        try:
            # Read from the identity map; only a rollback for an earlier row makes this a query.
            row = await session.get(ServiceFreeze, freeze_id)
            if row is not None and await _advance(session, row, now) == BY_DEADLINE:
                resumed_ids.append(freeze_id)
        except Exception:  # noqa: BLE001 - per-row isolation in a background job; the row is retried
            logger.warning("freeze step failed freeze=%s", freeze_id, exc_info=True)
            await session.rollback()
    return await _reload(session, resumed_ids)


async def _reload(session: AsyncSession, freeze_ids: list[int]) -> list[ServiceFreeze]:
    """The rows again, fresh: a later row's rollback expired the ones already done."""
    if not freeze_ids:
        return []
    rows = await session.scalars(
        select(ServiceFreeze).where(ServiceFreeze.id.in_(freeze_ids)).order_by(ServiceFreeze.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)
