"""Legacy soft-cancel policy helpers, and turning that path off for good.

Soft-cancel used to cancel abandoned pending orders in place from
``pending_order_cleanup_*`` settings. The finance cleanup tab is now tag-based
permanent deletion (``orders_purge``). The hourly purge job calls
``disable_all_legacy_soft_cancel`` so shops that still have soft-cancel on stop
cancelling without silently switching them to automatic deletion. Tag purge is
never written here — an operator with ``orders_purge`` must choose tags and auto
on purpose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, ResellerSetting
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)

MIN_HOURS = 1
MAX_HOURS = 720
DEFAULT_HOURS = 48
DEFAULT_APPROVAL_HOURS = 72
KEY_ENABLED = "pending_order_cleanup_enabled"
KEY_LAST_RUN = "pending_order_cleanup_last_run"
KEY_LAST_COUNT = "pending_order_cleanup_last_count"
# Keyset page size when scanning shops that still have soft-cancel on.
SHOP_SCAN_PAGE = 2000

UNPAID_STATUSES = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.AWAITING_RECEIPT.value,
        OrderStatus.REJECTED.value,
    }
)


@dataclass(frozen=True, slots=True)
class StaleOrderPolicy:
    """``approval_hours`` is None when orders waiting for receipt approval are never cancelled."""

    hours: int
    approval_hours: int | None = None


def clamp_hours(raw: object, default: int) -> int:
    try:
        hours = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        hours = default
    return max(MIN_HOURS, min(MAX_HOURS, hours))


def policy_from_settings(ui: dict[str, str]) -> StaleOrderPolicy:
    from app.services.users import on

    hours = clamp_hours(ui.get("pending_order_ttl_hours"), DEFAULT_HOURS)
    approval_hours = None
    if on(ui.get("pending_order_cleanup_awaiting_approval", "0")):
        approval_hours = clamp_hours(ui.get("pending_order_approval_ttl_hours"), DEFAULT_APPROVAL_HOURS)
    return StaleOrderPolicy(hours=hours, approval_hours=approval_hours)


def cancellable_statuses(policy: StaleOrderPolicy) -> frozenset[str]:
    if policy.approval_hours is None:
        return UNPAID_STATUSES
    return UNPAID_STATUSES | {OrderStatus.AWAITING_APPROVAL.value}


def stale_orders_clause(policy: StaleOrderPolicy, *, now: datetime | None = None) -> ColumnElement[bool]:
    now = now or datetime.now(timezone.utc)
    stale_unpaid = and_(
        Order.status.in_(tuple(UNPAID_STATUSES)),
        Order.created_at < now - timedelta(hours=policy.hours),
    )
    if policy.approval_hours is None:
        return stale_unpaid
    stale_awaiting = and_(
        Order.status == OrderStatus.AWAITING_APPROVAL.value,
        Order.created_at < now - timedelta(hours=policy.approval_hours),
    )
    return or_(stale_unpaid, stale_awaiting)


def scope_to_shop(query, reseller_id: int | None):
    if reseller_id is None:
        return query.where(Order.reseller_id.is_(None))
    return query.where(Order.reseller_id == int(reseller_id))


async def count_stale_orders(
    session: AsyncSession, policy: StaleOrderPolicy, *, reseller_id: int | None
) -> int:
    query = select(func.count(Order.id)).where(stale_orders_clause(policy))
    return int(await session.scalar(scope_to_shop(query, reseller_id)) or 0)


async def record_cleanup_run(session: AsyncSession, *, cancelled: int, reseller_id: int | None) -> None:
    from app.services.users import set_settings_bulk

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await set_settings_bulk(
        session, {KEY_LAST_RUN: stamp, KEY_LAST_COUNT: str(int(cancelled))}, reseller_id=reseller_id
    )


async def cleanup_status(session: AsyncSession, ui: dict[str, str], *, reseller_id: int | None) -> dict[str, object]:
    """Preview helpers for tests of the legacy soft-cancel policy."""
    policy = policy_from_settings(ui)
    last_run = (ui.get(KEY_LAST_RUN) or "").strip()
    last_count = str(ui.get(KEY_LAST_COUNT) or "0")
    return {
        "ready_count": await count_stale_orders(session, policy, reseller_id=reseller_id),
        "last_run": last_run.replace("T", " ")[:16] if last_run else "",
        "last_count": int(last_count) if last_count.isdigit() else 0,
        "hours": policy.hours,
        "approval_hours": policy.approval_hours,
    }


async def disable_legacy_soft_cancel(session: AsyncSession, shop_id: int | None) -> bool:
    """Turn off soft-cancel for one shop. Does not touch purge tags or auto-delete. Idempotent."""
    from app.services.users import get_all_settings, on, set_settings_bulk

    settings = await get_all_settings(session, reseller_id=shop_id)
    if not on(settings.get(KEY_ENABLED, "0")):
        return False
    await set_settings_bulk(session, {KEY_ENABLED: "0"}, reseller_id=shop_id)
    logger.info("disabled legacy soft-cancel shop=%s", shop_id)
    return True


async def _shops_with_soft_cancel_page(session: AsyncSession, *, after_id: int) -> list[int]:
    rows = await session.scalars(
        select(ResellerSetting.reseller_user_id)
        .where(
            ResellerSetting.key == KEY_ENABLED,
            ResellerSetting.value.in_(("1", "true", "yes", "on")),
            ResellerSetting.reseller_user_id > after_id,
        )
        .order_by(ResellerSetting.reseller_user_id)
        .limit(SHOP_SCAN_PAGE)
    )
    return [int(shop_id) for shop_id in rows.all()]


async def disable_all_legacy_soft_cancel(session: AsyncSession) -> int:
    """Platform + every shop still on soft-cancel, keyset-paged; one failure does not stop the rest.

    Scans in pages of ``SHOP_SCAN_PAGE`` so shops beyond the first page are not skipped.
    """
    done = 0
    try:
        done += int(await disable_legacy_soft_cancel(session, None))
    except Exception:
        logger.exception("soft-cancel disable failed shop=%s", None)
        await rollback_quiet(session)
    after_id = 0
    while True:
        page = await _shops_with_soft_cancel_page(session, after_id=after_id)
        if not page:
            break
        for shop_id in page:
            try:
                done += int(await disable_legacy_soft_cancel(session, shop_id))
            except Exception:
                logger.exception("soft-cancel disable failed shop=%s", shop_id)
                await rollback_quiet(session)
        after_id = page[-1]
        if len(page) < SHOP_SCAN_PAGE:
            break
    return done
