"""Volume allocation gate for fixed-plan reseller shops.

Ceiling comes from ``PgAdminSubscription`` (base_gb + extra_gb_purchased). Allocation is the SQL
sum of non-cancelled ``UserService.quota_data_limit_bytes`` of the shop, its live
``VolumeHold`` rows, ``ResellerProfile.external_allocated_bytes`` (panel users without a
local row, counted by ``volume_allocation_external``) and, for a level-1 shop, the ceilings of
its active level-2 child shops (their subscription totals; see ``volume_allocation_cascade``). Unlimited (0) and unsynced (NULL) services count as zero and are listed as
warnings. PAYG shops, shops with ``allow_volume_oversell`` and shops without a ceiling (no
subscription, or an unlimited one) are not gated.

A sale that is about to call the panel takes a hold first (``hold_volume``): lock, insert the
hold, check the total including it, commit. The hold is released in the same commit that writes
the service's new limit, or right away on a definite panel failure. A hold whose panel outcome
is unknown is left to expire, so a timeout never frees volume the panel may have granted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.integration_operation_models import IntegrationCapacityCommitment
from app.db.models import OrgPrincipal, PgAdminSubscription, ResellerProfile, UserService, VolumeHold
from app.services.billing import BILLING_MODE_PAYG, GB, is_payg
from app.services.formatting import format_bytes
from app.services.integration_runtime import integration_capacity_retained
from app.services.org_principals import DEPTH_TWO, STATUS_ACTIVE

logger = logging.getLogger(__name__)

# Longer than any panel call plus a delivery retry; an unknown outcome is counted until then.
HOLD_TTL = timedelta(hours=2)

CUSTOMER_UNAVAILABLE_MSG = "فعلاً موجود نیست"
LIMIT_UNKNOWN_MSG = "حجم این سرویس مشخص نیست؛ فروش بیش از سقف خاموش است و بدون حجم مشخص نمی‌شود فروخت"
UNLIMITED_BLOCKED_MSG = "ساخت یا تبدیل به سرویس نامحدود وقتی فروش بیش از سقف خاموش است مجاز نیست"

Audience = Literal["admin", "customer"]


class VolumeAllocationError(Exception):
    """Expected volume-budget rejection; routes translate ``message`` for the audience."""

    def __init__(
        self,
        message: str,
        *,
        allocated_bytes: int | None = None,
        ceiling_bytes: int | None = None,
        for_customer: bool = False,
    ) -> None:
        self.message = message
        self.allocated_bytes = allocated_bytes
        self.ceiling_bytes = ceiling_bytes
        self.for_customer = for_customer
        super().__init__(message)

    def user_message(self) -> str:
        return CUSTOMER_UNAVAILABLE_MSG if self.for_customer else self.message


@dataclass(frozen=True, slots=True)
class AllocationSnapshot:
    shop_id: int
    allocated_bytes: int
    external_allocated_bytes: int
    external_synced_at: datetime | None
    ceiling_bytes: int | None
    allow_oversell: bool
    is_payg: bool
    expired_held_bytes: int
    unlimited_services: list[dict[str, Any]]
    is_oversold: bool


@dataclass(frozen=True, slots=True)
class HeldVolume:
    """Handle of a committed hold, passed back to ``release_volume_hold``."""

    shop_id: int
    key: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_allocation_full_message(*, allocated_bytes: int, ceiling_bytes: int) -> str:
    """Admin/reseller-facing full message (never shown to end customers)."""
    return f"سقف حجم پر است: {format_bytes(allocated_bytes)} از {format_bytes(ceiling_bytes)}"


def _rejection(message: str, audience: Audience, **numbers: int) -> VolumeAllocationError:
    return VolumeAllocationError(message, for_customer=(audience == "customer"), **numbers)


async def lock_reseller_profile(session: AsyncSession, shop_id: int) -> ResellerProfile | None:
    """Row-lock the shop profile (FOR UPDATE on PostgreSQL; no-op on SQLite)."""
    return await session.scalar(
        select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)).with_for_update()
    )


async def ceiling_bytes_for_profile(session: AsyncSession, profile: ResellerProfile) -> int | None:
    """The shop's ceiling in bytes; None when it has none (no subscription, or unlimited)."""
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        return None
    sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == uname))
    if sub is None:
        return None
    total_gb = int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0)
    return total_gb * int(GB) if total_gb > 0 else None


async def _gated_ceiling(session: AsyncSession, shop_id: int, *, lock: bool) -> int | None:
    """The ceiling when the gate applies to this shop, else None."""
    if lock:
        profile = await lock_reseller_profile(session, shop_id)
    else:
        profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    if profile is None or is_payg(profile) or bool(profile.allow_volume_oversell):
        return None
    return await ceiling_bytes_for_profile(session, profile)


async def shop_is_gated(session: AsyncSession, shop_id: int) -> bool:
    """Whether sales of this shop are held against a volume ceiling."""
    return await _gated_ceiling(session, int(shop_id), lock=False) is not None


async def gated_ceiling_locked(session: AsyncSession, shop_id: int) -> int | None:
    """Lock the shop profile for the caller's transaction and return its ceiling when gated."""
    return await _gated_ceiling(session, int(shop_id), lock=True)


async def sum_service_bytes(session: AsyncSession, shop_id: int) -> int:
    """SQL aggregate of positive limits of the shop's non-cancelled services."""
    total = await session.scalar(
        select(func.coalesce(func.sum(UserService.quota_data_limit_bytes), 0)).where(
            UserService.shop_id == int(shop_id),
            UserService.is_cancelled.is_(False),
            UserService.quota_data_limit_bytes > 0,
        )
    )
    return int(total or 0)


async def sum_active_holds(session: AsyncSession, shop_id: int) -> int:
    if not integration_capacity_retained():
        total = await session.scalar(select(func.coalesce(func.sum(VolumeHold.bytes), 0)).where(
            VolumeHold.shop_id == int(shop_id), VolumeHold.expires_at > _utcnow(),
        ))
        return int(total or 0)
    temporary = select(func.coalesce(func.sum(VolumeHold.bytes), 0)).where(
        VolumeHold.shop_id == int(shop_id), VolumeHold.expires_at > _utcnow(),
    ).scalar_subquery()
    durable = select(func.coalesce(func.sum(IntegrationCapacityCommitment.bytes), 0)).where(
        IntegrationCapacityCommitment.shop_id == int(shop_id), IntegrationCapacityCommitment.active.is_(True),
    ).scalar_subquery()
    return int(await session.scalar(select(temporary + durable)) or 0)


async def sum_external_bytes(session: AsyncSession, shop_id: int) -> int:
    """Last counted volume of the shop's panel users that have no local service row."""
    total = await session.scalar(
        select(ResellerProfile.external_allocated_bytes).where(ResellerProfile.user_id == int(shop_id))
    )
    return int(total or 0)


def child_ceilings_query(parent_shop_ids: list[int]) -> Select[Any]:
    """``(parent_shop_id, child_shop_id, child_ceiling_gb)`` of the active level-2 children.

    A child's ceiling is its own subscription total; a child without one (or with 0) is
    uncapped: it adds nothing to the parent and is listed as a warning instead.
    """
    child, parent = aliased(OrgPrincipal), aliased(OrgPrincipal)
    child_profile, parent_profile = aliased(ResellerProfile), aliased(ResellerProfile)
    total_gb = func.coalesce(PgAdminSubscription.base_gb, 0) + func.coalesce(PgAdminSubscription.extra_gb_purchased, 0)
    return (
        select(parent_profile.user_id, child_profile.user_id, total_gb)
        .select_from(child)
        .join(parent, parent.id == child.parent_id)
        .join(parent_profile, parent_profile.id == parent.reseller_profile_id)
        .join(child_profile, child_profile.id == child.reseller_profile_id)
        .outerjoin(PgAdminSubscription, PgAdminSubscription.pg_username == child_profile.pg_admin_username)
        .where(
            child.depth == DEPTH_TWO,
            child.status == STATUS_ACTIVE,
            parent_profile.user_id.in_([int(x) for x in parent_shop_ids]),
        )
    )


async def child_ceiling_gb_by_parent(session: AsyncSession, parent_shop_ids: list[int]) -> dict[int, int]:
    """Sum of capped children's ceilings in GB per parent shop (one query)."""
    totals: dict[int, int] = {}
    for parent_id, _child_id, gb in (await session.execute(child_ceilings_query(parent_shop_ids))).all():
        if int(gb or 0) > 0:
            totals[int(parent_id)] = totals.get(int(parent_id), 0) + int(gb)
    return totals


async def sum_child_ceiling_bytes(session: AsyncSession, shop_id: int) -> int:
    """Volume a level-1 shop has passed on to its active level-2 children."""
    return (await child_ceiling_gb_by_parent(session, [int(shop_id)])).get(int(shop_id), 0) * int(GB)


async def sum_allocated_bytes(session: AsyncSession, shop_id: int) -> int:
    """Everything that counts against the ceiling: services, holds, panel-only users, children."""
    return (
        await sum_service_bytes(session, shop_id)
        + await sum_active_holds(session, shop_id)
        + await sum_external_bytes(session, shop_id)
        + await sum_child_ceiling_bytes(session, shop_id)
    )


async def sum_expired_held_bytes(session: AsyncSession, shop_id: int) -> int:
    """Volume still held by expired (but not cancelled) services."""
    total = await session.scalar(
        select(func.coalesce(func.sum(UserService.quota_data_limit_bytes), 0)).where(
            UserService.shop_id == int(shop_id),
            UserService.is_cancelled.is_(False),
            UserService.quota_data_limit_bytes > 0,
            UserService.quota_expire_at.is_not(None),
            UserService.quota_expire_at < _utcnow(),
        )
    )
    return int(total or 0)


async def list_unlimited_or_unsynced_services(
    session: AsyncSession, shop_id: int, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Services that count as zero and should be warned about on the capacity page."""
    rows = (
        await session.execute(
            select(UserService.id, UserService.pg_username, UserService.quota_data_limit_bytes)
            .where(
                UserService.shop_id == int(shop_id),
                UserService.is_cancelled.is_(False),
                or_(UserService.quota_data_limit_bytes.is_(None), UserService.quota_data_limit_bytes == 0),
            )
            .order_by(UserService.id.desc())
            .limit(int(limit))
        )
    ).all()
    return [
        {"id": int(sid), "pg_username": str(uname or ""), "kind": "unsynced" if limit_b is None else "unlimited"}
        for sid, uname, limit_b in rows
    ]


def _intended_delta(
    delta_bytes: int | None, *, allow_unlimited: bool, audience: Audience
) -> int:
    """Validate the requested change: None is unknown, 0 is an unlimited service."""
    if delta_bytes is None:
        raise _rejection(LIMIT_UNKNOWN_MSG, audience)
    if isinstance(delta_bytes, bool) or not isinstance(delta_bytes, int):
        raise _rejection(LIMIT_UNKNOWN_MSG, audience)
    if delta_bytes == 0 and not allow_unlimited:
        raise _rejection(UNLIMITED_BLOCKED_MSG, audience)
    return delta_bytes


def _assert_fits(allocated: int, ceiling: int, audience: Audience) -> None:
    if allocated > ceiling:
        raise _rejection(
            format_allocation_full_message(allocated_bytes=allocated, ceiling_bytes=ceiling),
            audience,
            allocated_bytes=allocated,
            ceiling_bytes=ceiling,
        )


async def assert_volume_delta_allowed(
    session: AsyncSession,
    *,
    shop_id: int | None,
    delta_bytes: int | None,
    audience: Audience = "admin",
    allow_unlimited: bool = False,
    lock: bool = False,
) -> None:
    """Check without a hold: before an order is created, or with ``lock`` inside a caller's
    transaction that writes the new limit before it commits.

    ``delta_bytes`` None is an unknown limit and 0 an unlimited service; both are refused while
    the gate applies. Negative deltas (shrink) always pass.
    """
    if shop_id is None:
        return
    ceiling = await _gated_ceiling(session, int(shop_id), lock=lock)
    if ceiling is None:
        return
    delta = _intended_delta(delta_bytes, allow_unlimited=allow_unlimited, audience=audience)
    if delta <= 0:
        return
    _assert_fits(await sum_allocated_bytes(session, int(shop_id)) + delta, ceiling, audience)


async def hold_volume(
    session: AsyncSession,
    *,
    shop_id: int | None,
    delta_bytes: int | None,
    key: str,
    audience: Audience = "admin",
    allow_unlimited: bool = False,
    ttl: timedelta = HOLD_TTL,
) -> HeldVolume | None:
    """Reserve ``delta_bytes`` for an upcoming panel call and commit; None when nothing is held.

    The hold row is inserted before the total is read, so on SQLite its write lock and on
    PostgreSQL the profile row lock make concurrent holds of one shop take turns. Re-using
    ``key`` replaces the earlier hold (a retried delivery does not count twice).
    """
    if shop_id is None:
        return None
    sid = int(shop_id)
    ceiling = await _gated_ceiling(session, sid, lock=True)
    if ceiling is None:
        return None
    delta = _intended_delta(delta_bytes, allow_unlimited=allow_unlimited, audience=audience)
    if delta <= 0:
        return None
    now = _utcnow()
    hold_key = key[:96]
    # Savepoint: a refused hold rolls back only itself, never the caller's pending work.
    try:
        async with session.begin_nested():
            await session.execute(
                delete(VolumeHold).where(
                    or_(VolumeHold.hold_key == hold_key, (VolumeHold.shop_id == sid) & (VolumeHold.expires_at <= now))
                )
            )
            session.add(VolumeHold(shop_id=sid, hold_key=hold_key, bytes=delta, expires_at=now + ttl))
            await session.flush()
            _assert_fits(await sum_allocated_bytes(session, sid), ceiling, audience)
    finally:
        # Ends the profile lock either way; a refusal keeps only the caller's own pending work.
        await session.commit()
    return HeldVolume(shop_id=sid, key=hold_key)


async def release_volume_hold(session: AsyncSession, hold: HeldVolume | None) -> None:
    """Drop a hold; the caller commits it together with the service's new limit."""
    if hold is not None:
        await session.execute(delete(VolumeHold).where(VolumeHold.hold_key == hold.key))


async def record_service_limit(session: AsyncSession, *, shop_id: int, pg_user_id: int, data_limit: int) -> bool:
    """Write a limit just set on the panel into the shop's local service row; False if none."""
    result = await session.execute(
        update(UserService)
        .where(
            UserService.shop_id == int(shop_id),
            UserService.pg_user_id == int(pg_user_id),
            UserService.is_cancelled.is_(False),
        )
        .values(quota_data_limit_bytes=max(0, int(data_limit)))
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def build_allocation_snapshot(
    session: AsyncSession, shop_id: int
) -> AllocationSnapshot | None:
    profile = await session.scalar(
        select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id))
    )
    if profile is None:
        return None
    payg = is_payg(profile)
    ceiling = await ceiling_bytes_for_profile(session, profile)
    allocated = await sum_allocated_bytes(session, shop_id)
    expired_held = await sum_expired_held_bytes(session, shop_id)
    unlimited = await list_unlimited_or_unsynced_services(session, shop_id)
    allow = bool(getattr(profile, "allow_volume_oversell", True))
    oversold = (
        (not payg)
        and (not allow)
        and ceiling is not None
        and allocated > ceiling
    )
    return AllocationSnapshot(
        shop_id=int(shop_id),
        allocated_bytes=allocated,
        external_allocated_bytes=int(profile.external_allocated_bytes or 0),
        external_synced_at=profile.external_alloc_synced_at,
        ceiling_bytes=ceiling,
        allow_oversell=allow,
        is_payg=payg,
        expired_held_bytes=expired_held,
        unlimited_services=unlimited,
        is_oversold=oversold,
    )


# Gated shops are few (one per reseller); this bounds one batch of the dashboard and the alerts.
GATED_SHOPS_BATCH = 1000


async def gated_allocations(session: AsyncSession, *, limit: int = GATED_SHOPS_BATCH) -> list[tuple[int, int, int]]:
    """``(shop_id, allocated_bytes, ceiling_bytes)`` of every gated shop with a ceiling, in 5 queries."""
    profiles = (
        await session.execute(
            select(
                ResellerProfile.user_id, ResellerProfile.pg_admin_username, ResellerProfile.external_allocated_bytes
            )
            .where(
                ResellerProfile.is_active.is_(True),
                ResellerProfile.allow_volume_oversell.is_(False),
                func.lower(ResellerProfile.billing_mode) != BILLING_MODE_PAYG,
                ResellerProfile.pg_admin_username.is_not(None),
            )
            .order_by(ResellerProfile.user_id)
            .limit(int(limit))
        )
    ).all()
    if not profiles:
        return []
    subs = (
        await session.execute(
            select(PgAdminSubscription.pg_username, PgAdminSubscription.base_gb, PgAdminSubscription.extra_gb_purchased)
            .where(PgAdminSubscription.pg_username.in_({str(u).strip() for _, u, _ in profiles}))
        )
    ).all()
    ceiling_by_uname = {
        str(uname): (int(base or 0) + int(extra or 0)) * int(GB)
        for uname, base, extra in subs
        if int(base or 0) + int(extra or 0) > 0
    }
    shop_ids = [int(sid) for sid, _, _ in profiles]
    service_sums = dict(
        (
            await session.execute(
                select(UserService.shop_id, func.coalesce(func.sum(UserService.quota_data_limit_bytes), 0))
                .where(
                    UserService.shop_id.in_(shop_ids),
                    UserService.is_cancelled.is_(False),
                    UserService.quota_data_limit_bytes > 0,
                )
                .group_by(UserService.shop_id)
            )
        ).all()
    )
    hold_sums = dict(
        (
            await session.execute(
                select(VolumeHold.shop_id, func.coalesce(func.sum(VolumeHold.bytes), 0))
                .where(VolumeHold.shop_id.in_(shop_ids), VolumeHold.expires_at > _utcnow())
                .group_by(VolumeHold.shop_id)
            )
        ).all()
    )
    child_gb = await child_ceiling_gb_by_parent(session, shop_ids)
    out: list[tuple[int, int, int]] = []
    for sid, uname, external in profiles:
        ceiling = ceiling_by_uname.get(str(uname).strip())
        if ceiling is not None:
            allocated = (
                int(service_sums.get(sid) or 0)
                + int(hold_sums.get(sid) or 0)
                + int(external or 0)
                + child_gb.get(int(sid), 0) * int(GB)
            )
            out.append((int(sid), allocated, ceiling))
    return out


async def list_oversold_shop_ids(session: AsyncSession, *, limit: int = GATED_SHOPS_BATCH) -> list[int]:
    """Gated shops whose allocation already exceeds their ceiling (Owner dashboard)."""
    return [sid for sid, allocated, ceiling in await gated_allocations(session, limit=limit) if allocated > ceiling]


def is_hard_panel_rejection(exc: BaseException) -> bool:
    """True when the panel definitely refused the mutation (safe to release reservation)."""
    from app.services.pasarguard import PasarGuardError

    return isinstance(exc, PasarGuardError) and exc.status_code in (
        400,
        401,
        403,
        404,
        409,
        422,
    )


def apply_oversell_switch(profile: ResellerProfile, *, shown: bool, allow: bool, actor_is_owner: bool) -> None:
    """Apply the edit form's oversell switch; only the platform Owner may change it."""
    if shown and actor_is_owner:
        profile.allow_volume_oversell = bool(allow)


OVERSELL_OWNER_ONLY_MSG = "فقط مالک سیستم می‌تواند فروش بیش از سقف حجم را تغییر دهد"
OVERSELL_PAYG_MSG = "نمایندهٔ PAYG سقف حجم ندارد"


async def set_shop_oversell(session: AsyncSession, shop_id: int, *, allow: bool, actor_is_owner: bool) -> bool:
    """Set and commit a shop's oversell switch (create form, bot card); return the new value."""
    if not actor_is_owner:
        raise ValueError(OVERSELL_OWNER_ONLY_MSG)
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    if profile is None:
        raise ValueError("نماینده یافت نشد")
    if is_payg(profile):
        raise ValueError(OVERSELL_PAYG_MSG)
    profile.allow_volume_oversell = bool(allow)
    await session.commit()
    return bool(allow)


async def apply_create_form_oversell(
    session: AsyncSession, shop_id: int | None, *, form_value: object, actor_is_owner: bool
) -> None:
    """Turn oversell on for a shop just created from a form whose switch was checked.

    New shops start with oversell off, so an unchecked switch, a non-owner actor or a PAYG
    shop changes nothing.
    """
    if shop_id is None or not form_value or not actor_is_owner:
        return
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    if profile is None or is_payg(profile):
        return
    profile.allow_volume_oversell = True
    await session.commit()


VOLUME_ALERT = "volume_alloc"


def threshold_level(allocated_bytes: int, ceiling_bytes: int) -> int:
    """0 below 80%, 1 from 80%, 2 from 100% (integer maths, no float)."""
    if allocated_bytes * 100 >= ceiling_bytes * 100:
        return 2
    return 1 if allocated_bytes * 100 >= ceiling_bytes * 80 else 0


def _threshold_text(level: int, allocated_bytes: int, ceiling_bytes: int) -> str:
    amounts = f"({format_bytes(allocated_bytes)} از {format_bytes(ceiling_bytes)})"
    if level >= 2:
        return f"⛔ سقف حجم فروشگاه شما پر شد {amounts}؛ تا آزاد شدن یا افزایش سقف، فروش جدید ممکن نیست."
    return f"⚠️ ۸۰٪ سقف حجم فروشگاه شما فروخته شده است {amounts}."


async def volume_threshold_signals(session: AsyncSession) -> list[Any]:
    """One alert per gated shop; it is announced when it reaches 80% or 100% and resets below 80%.

    ``count`` is the level, so ``claim_signal`` announces each level once and a level-0 signal
    clears the state for the next time the shop fills up.
    """
    from app.services.panel_alert_notifications import Signal

    return [
        Signal(
            shop_id=sid,
            alert=VOLUME_ALERT,
            notify_key="notify_panel_capacity",
            count=(level := threshold_level(allocated, ceiling)),
            text=_threshold_text(level, allocated, ceiling),
            path="/resellers",
        )
        for sid, allocated, ceiling in await gated_allocations(session)
    ]
