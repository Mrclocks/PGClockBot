"""Re-read the existing limits of resellers granted with «ظرفیت فعلی» whose capacity was saved as zero.

Before the grant checked its reads, a PasarGuard read that failed (or a limit below 1 GB) was
stored as 0 GB / 0 users, which the panel shows as unlimited. Subscription rows that never got a
``plan_id`` are linked from the shop profile first, so the current-capacity filter and the sales
ceiling can see the plan. Only rows with a zero ``base_gb`` are capacity-healed, and a row is
changed only when the admin's live limits are readable and non-zero.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.pg_admin_subscription import (
    PLAN_KIND_CURRENT_CAPACITY,
    STATUS_ACTIVE,
    PgCapacityUnreadable,
    read_live_pg_admin_capacity,
)

logger = logging.getLogger(__name__)

REPAIR_BATCH = 200
READ_CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class RepairReport:
    fixed: int = 0
    unreadable: int = 0
    unlimited: int = 0
    plan_linked: int = 0


async def _read(username: str, gate: asyncio.Semaphore) -> tuple[int, int] | None:
    async with gate:
        try:
            return await read_live_pg_admin_capacity(username)
        except PgCapacityUnreadable:
            return None


async def backfill_subscription_plan_ids(session: AsyncSession) -> int:
    """Copy ``ResellerProfile.plan_id`` onto subscription rows that have none (up to ``REPAIR_BATCH``)."""
    rows = (
        await session.execute(
            select(PgAdminSubscription.id, ResellerProfile.plan_id)
            .join(
                ResellerProfile,
                ResellerProfile.pg_admin_username == PgAdminSubscription.pg_username,
            )
            .where(
                PgAdminSubscription.plan_id.is_(None),
                ResellerProfile.plan_id.is_not(None),
            )
            .order_by(PgAdminSubscription.id)
            .limit(REPAIR_BATCH)
        )
    ).all()
    linked = 0
    for sub_id, plan_id in rows:
        result = await session.execute(
            update(PgAdminSubscription)
            .where(
                PgAdminSubscription.id == int(sub_id),
                PgAdminSubscription.plan_id.is_(None),
            )
            .values(plan_id=int(plan_id))
            .execution_options(synchronize_session=False)
        )
        linked += int(result.rowcount or 0)
    return linked


async def repair_current_capacity_snapshots(session: AsyncSession) -> RepairReport:
    """Fix up to ``REPAIR_BATCH`` zero volume snapshots; run again for more. Panel reads are bounded.

    Rows that already bought extras keep those extras: live panel GB minus ``extra_gb_purchased``
    becomes ``base_gb``, so the sales ceiling does not double-count. User slots still require a
    zero ``base_users`` before they are overwritten (extras for users are rare on this path).
    Missing ``plan_id`` values are filled from the shop profile before the capacity pass.
    """
    plan_linked = await backfill_subscription_plan_ids(session)
    rows = (
        await session.execute(
            select(
                PgAdminSubscription.id,
                PgAdminSubscription.pg_username,
                PgAdminSubscription.extra_gb_purchased,
                PgAdminSubscription.base_users,
            )
            .join(ResellerPlan, ResellerPlan.id == PgAdminSubscription.plan_id)
            .where(
                ResellerPlan.plan_kind == PLAN_KIND_CURRENT_CAPACITY,
                PgAdminSubscription.access_status == STATUS_ACTIVE,
                PgAdminSubscription.base_gb == 0,
            )
            .order_by(PgAdminSubscription.id)
            .limit(REPAIR_BATCH)
        )
    ).all()
    gate = asyncio.Semaphore(READ_CONCURRENCY)
    live = await asyncio.gather(*(_read(name, gate) for _id, name, _extra, _users in rows))
    fixed = unreadable = unlimited = 0
    for (sub_id, _name, extra_gb, base_users), capacity in zip(rows, live, strict=True):
        if capacity is None:
            unreadable += 1
        elif capacity == (0, 0):
            unlimited += 1
        else:
            live_gb, live_users = int(capacity[0]), int(capacity[1])
            healed_base = max(0, live_gb - max(0, int(extra_gb or 0)))
            values: dict[str, int] = {"base_gb": healed_base}
            # Only fill users when the recorded base users is still zero, so a partial snapshot
            # that already has users is not overwritten by a later live read.
            if int(base_users or 0) == 0 and live_users > 0:
                values["base_users"] = live_users
            result = await session.execute(
                update(PgAdminSubscription)
                .where(
                    PgAdminSubscription.id == sub_id,
                    PgAdminSubscription.base_gb == 0,
                )
                .values(**values),
                execution_options={"synchronize_session": False},
            )
            fixed += int(result.rowcount or 0)
    await session.commit()
    logger.info(
        "current capacity repair fixed=%s unreadable=%s unlimited=%s plan_linked=%s",
        fixed,
        unreadable,
        unlimited,
        plan_linked,
    )
    return RepairReport(
        fixed=fixed, unreadable=unreadable, unlimited=unlimited, plan_linked=plan_linked
    )
