"""Shared PG-admin subscription: time expiry, capacity extras, renew invoice.

Clock is keyed by PasarGuard username so reseller and pg_staff share one period.
PAYG profiles are skipped (no time gate). Mid-period add-ons do not extend expires_at.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    PgAdminSubscription,
    PgStaffAccess,
    ResellerPlan,
    ResellerProfile,
)
from app.services.billing import GB, is_payg
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_toman
from app.services.volume_allocation import is_hard_panel_rejection
from app.services.volume_allocation_cascade import assert_child_growth_fits, assert_parent_has_room

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"

PLAN_KIND_SUBSCRIPTION = "subscription"
PLAN_KIND_CURRENT_CAPACITY = "current_capacity"
PLAN_KIND_ADDON_VOLUME = "addon_volume"
PLAN_KIND_ADDON_USERS = "addon_users"

# Clock-bearing kinds (time expiry / renew). Addons are mid-period packs only.
_CLOCK_PLAN_KINDS = frozenset({PLAN_KIND_SUBSCRIPTION, PLAN_KIND_CURRENT_CAPACITY})

RENEW_MODE_FIXED = "fixed"
RENEW_MODE_FROM_CAPACITY = "from_capacity"


def resolve_renew_pricing_mode(plan: ResellerPlan | None) -> str:
    """Effective renew mode. Buy-extra plans always bill from capacity (anti-abuse)."""
    if plan is not None and bool(getattr(plan, "allow_buy_extra", False)):
        return RENEW_MODE_FROM_CAPACITY
    mode = str(getattr(plan, "renew_pricing_mode", None) or RENEW_MODE_FIXED).strip().lower()
    if mode != RENEW_MODE_FROM_CAPACITY:
        return RENEW_MODE_FIXED
    return mode

WARN_DAYS_BEFORE = 3


def normalize_pg_username(username: str | None) -> str:
    return (username or "").strip()


def plan_kind_of(plan: ResellerPlan | None) -> str:
    if plan is None:
        return PLAN_KIND_SUBSCRIPTION
    kind = str(getattr(plan, "plan_kind", None) or PLAN_KIND_SUBSCRIPTION).strip().lower()
    if kind in {
        PLAN_KIND_SUBSCRIPTION,
        PLAN_KIND_CURRENT_CAPACITY,
        PLAN_KIND_ADDON_VOLUME,
        PLAN_KIND_ADDON_USERS,
    }:
        return kind
    return PLAN_KIND_SUBSCRIPTION


def is_subscription_plan(plan: ResellerPlan | None) -> bool:
    """True for clock-bearing packages (standard subscription + current_capacity)."""
    return plan_kind_of(plan) in _CLOCK_PLAN_KINDS


def is_current_capacity_plan(plan: ResellerPlan | None) -> bool:
    return plan_kind_of(plan) == PLAN_KIND_CURRENT_CAPACITY


def is_addon_plan(plan: ResellerPlan | None) -> bool:
    return plan_kind_of(plan) in {PLAN_KIND_ADDON_VOLUME, PLAN_KIND_ADDON_USERS}


class PgCapacityUnreadable(ValueError):
    """The admin's limits could not be read from PasarGuard, so no capacity may be recorded."""


UNREADABLE_CAPACITY = "سقف حجم و کاربر این ادمین از پاسارگارد خوانده نشد؛ اتصال پنل را بررسی و دوباره تلاش کنید"


async def read_live_pg_admin_capacity(pg_username: str) -> tuple[int, int]:
    """Read the admin's live data limit and max users as ``(gb, users)``.

    ``0`` means unlimited on that axis. A limit that cannot be read raises
    ``PgCapacityUnreadable`` instead of looking like «unlimited», and a limit below one gigabyte
    rounds up so it never turns into «unlimited».
    """
    from app.services.pasarguard import get_pg
    from app.services.pg_quota import extract_admin_capacity

    uname = normalize_pg_username(pg_username)
    if not uname:
        raise PgCapacityUnreadable(UNREADABLE_CAPACITY)
    try:
        admin = await get_pg().get_admin(uname)
    except Exception:
        logger.warning("read live PG capacity failed admin=%s", uname, exc_info=True)
        raise PgCapacityUnreadable(UNREADABLE_CAPACITY) from None
    if not isinstance(admin, dict) or not admin:
        raise PgCapacityUnreadable(UNREADABLE_CAPACITY)
    role = admin.get("role") if isinstance(admin.get("role"), dict) else None
    data_limit_bytes, users = extract_admin_capacity(admin, role)
    gb = -(-data_limit_bytes // GB) if data_limit_bytes > 0 else 0
    return gb, users


def _parse_id_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    out: list[int] = []
    if isinstance(data, list):
        for x in data:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
    return out


def _dump_id_list(ids: list[int]) -> str:
    return json.dumps([int(x) for x in ids], separators=(",", ":"))


def _total_gb(sub: PgAdminSubscription | None) -> int:
    return int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0) if sub is not None else 0


def _total_users(sub: PgAdminSubscription | None) -> int:
    return int(sub.base_users or 0) + int(sub.extra_users_purchased or 0) if sub is not None else 0


def raise_subscription_total_to_live(sub: PgAdminSubscription, live_gb: int) -> bool:
    """Raise recorded base/extras so their sum covers the live panel GB. Never lowers.

    Bought capacity often lands on the panel first; if ``extra_gb_purchased`` was not written
    (or ``base_gb`` stayed 0), the sales gate would keep the old package size. When ``base_gb``
    is already set, growth beyond it is stored as extras; when base is still 0, live minus any
    existing extras becomes the base. Returns True when the row changed.
    """
    live = max(0, int(live_gb or 0))
    if live <= 0:
        return False
    base = max(0, int(sub.base_gb or 0))
    extra = max(0, int(sub.extra_gb_purchased or 0))
    if base + extra >= live:
        return False
    if base <= 0:
        sub.base_gb = max(0, live - extra)
    else:
        sub.extra_gb_purchased = live - base
    return True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def get_subscription(
    session: AsyncSession, pg_username: str
) -> PgAdminSubscription | None:
    uname = normalize_pg_username(pg_username)
    if not uname:
        return None
    result = await session.execute(
        select(PgAdminSubscription).where(PgAdminSubscription.pg_username == uname)
    )
    return result.scalar_one_or_none()


async def profile_plan_id_for_admin(session: AsyncSession, pg_username: str) -> int | None:
    """``ResellerProfile.plan_id`` for this panel admin, if any shop profile points at them."""
    uname = normalize_pg_username(pg_username)
    if not uname:
        return None
    plan_id = await session.scalar(
        select(ResellerProfile.plan_id)
        .where(
            ResellerProfile.pg_admin_username == uname,
            ResellerProfile.plan_id.is_not(None),
        )
        .order_by(ResellerProfile.id)
        .limit(1)
    )
    return int(plan_id) if plan_id else None


async def resolve_subscription_plan_id(
    session: AsyncSession, sub: PgAdminSubscription
) -> int | None:
    """Subscription plan first; if the row never got one, the linked shop profile's plan."""
    if sub.plan_id:
        return int(sub.plan_id)
    return await profile_plan_id_for_admin(session, sub.pg_username)


async def get_or_create_subscription(
    session: AsyncSession, pg_username: str
) -> PgAdminSubscription:
    uname = normalize_pg_username(pg_username)
    if not uname:
        raise ValueError("نام ادمین پاسارگارد نامعتبر است")
    sub = await get_subscription(session, uname)
    if sub:
        return sub
    # New rows inherit the shop profile's plan so ceiling fallbacks and renewals have a plan_id.
    sub = PgAdminSubscription(
        pg_username=uname,
        access_status=STATUS_ACTIVE,
        plan_id=await profile_plan_id_for_admin(session, uname),
    )
    session.add(sub)
    await session.flush()
    return sub


def renew_base_amount(plan: ResellerPlan | None) -> int:
    if plan is None:
        return 0
    renew = int(getattr(plan, "renew_price", 0) or 0)
    if renew > 0:
        return renew
    return max(0, int(getattr(plan, "price", 0) or 0))


def plan_carries_remaining_period(plan: ResellerPlan | None) -> bool:
    """Whether renew extends from a future expiry (default yes)."""
    if plan is None:
        return True
    return bool(getattr(plan, "carry_remaining_period", True))


def plan_keeps_extras_on_renew(plan: ResellerPlan | None) -> bool:
    """Whether purchased extra GB/user slots survive renew (default yes)."""
    if plan is None:
        return True
    return bool(getattr(plan, "keep_extras_on_renew", True))


def compute_renew_invoice(
    plan: ResellerPlan | None,
    sub: PgAdminSubscription | None,
) -> dict[str, Any]:
    """Server-side renew quote. Never trust client amounts.

    Mode ``from_capacity``:
      base + extra_gb × extra_gb_price + extra_users × extra_user_price
    Mode ``fixed`` (default): renew_price or plan.price

    If ``allow_buy_extra`` is on, mode is forced to ``from_capacity`` so renew
    cannot undercharge after capacity purchases.

    When ``keep_extras_on_renew`` is off, extras are not billed because renew
    clears them before the next period starts.
    """
    base = renew_base_amount(plan)
    keep_extras = plan_keeps_extras_on_renew(plan)
    raw_extra_gb = int(getattr(sub, "extra_gb_purchased", 0) or 0) if sub else 0
    raw_extra_users = int(getattr(sub, "extra_users_purchased", 0) or 0) if sub else 0
    extra_gb = raw_extra_gb if keep_extras else 0
    extra_users = raw_extra_users if keep_extras else 0
    gb_unit = max(0, int(getattr(plan, "extra_gb_price", 0) or 0)) if plan else 0
    user_unit = max(0, int(getattr(plan, "extra_user_price", 0) or 0)) if plan else 0
    mode = resolve_renew_pricing_mode(plan)
    extras_gb_amount = extra_gb * gb_unit if mode == RENEW_MODE_FROM_CAPACITY else 0
    extras_users_amount = extra_users * user_unit if mode == RENEW_MODE_FROM_CAPACITY else 0
    total = base + extras_gb_amount + extras_users_amount
    return {
        "mode": mode,
        "base": base,
        "extra_gb": extra_gb,
        "extra_users": extra_users,
        "extra_gb_unit": gb_unit,
        "extra_user_unit": user_unit,
        "extras_gb_amount": extras_gb_amount,
        "extras_users_amount": extras_users_amount,
        "total": max(0, int(total)),
        "base_gb": int(getattr(sub, "base_gb", 0) or 0) if sub else 0,
        "base_users": int(getattr(sub, "base_users", 0) or 0) if sub else 0,
        "total_gb": (int(getattr(sub, "base_gb", 0) or 0) + extra_gb) if sub else extra_gb,
        "total_users": (int(getattr(sub, "base_users", 0) or 0) + extra_users)
        if sub
        else extra_users,
        "expires_at": getattr(sub, "expires_at", None) if sub else None,
        "access_status": getattr(sub, "access_status", None) if sub else None,
        "duration_days": max(0, int(getattr(plan, "duration_days", 0) or 0)) if plan else 0,
        "carry_remaining_period": plan_carries_remaining_period(plan),
        "keep_extras_on_renew": keep_extras,
        "cleared_extra_gb": 0 if keep_extras else raw_extra_gb,
        "cleared_extra_users": 0 if keep_extras else raw_extra_users,
    }


def format_renew_invoice_html(invoice: dict[str, Any], *, currency: str = "تومان") -> str:
    lines = [
        f"مبلغ پایه تمدید: <b>{format_toman(int(invoice.get('base') or 0), currency)}</b>",
    ]
    if invoice.get("mode") == RENEW_MODE_FROM_CAPACITY:
        eg = int(invoice.get("extra_gb") or 0)
        eu = int(invoice.get("extra_users") or 0)
        if eg > 0:
            lines.append(
                f"حجم اضافه ({eg} گیگ): <b>"
                f"{format_toman(int(invoice.get('extras_gb_amount') or 0), currency)}</b>"
            )
        if eu > 0:
            lines.append(
                f"کاربر اضافه ({eu}): <b>"
                f"{format_toman(int(invoice.get('extras_users_amount') or 0), currency)}</b>"
            )
        lines.append(
            f"ظرفیت فعلی: <b>{int(invoice.get('total_users') or 0)}</b> کاربر · "
            f"<b>{int(invoice.get('total_gb') or 0)}</b> گیگ"
        )
    days = int(invoice.get("duration_days") or 0)
    if days > 0:
        lines.append(f"مدت دوره: <b>{days}</b> روز")
    lines.append(f"جمع قابل پرداخت: <b>{format_toman(int(invoice.get('total') or 0), currency)}</b>")
    return "\n".join(lines)


def plan_admin_limits(plan: ResellerPlan, *, current_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Panel admin fields for a package's volume and user cap; empty when the package sets none.

    PasarGuard replaces ``permission_overrides`` as a whole, so the admin's other override keys
    (for example a per-user device cap) are carried over.
    """
    return _capacity_payload(
        int(getattr(plan, "included_gb", 0) or 0),
        int(getattr(plan, "included_users", 0) or 0),
        current_overrides,
    )


def _capacity_payload(total_gb: int, total_users: int, current_overrides: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if total_gb > 0:
        payload["data_limit"] = int(total_gb * GB)
    if total_users > 0:
        overrides = {k: v for k, v in (current_overrides or {}).items() if v is not None}
        overrides["max_users"] = int(total_users)
        payload["max_users"] = int(total_users)
        payload["permission_overrides"] = overrides
    return payload


async def _apply_pg_capacity(
    pg,
    username: str,
    *,
    total_gb: int,
    total_users: int,
) -> None:
    """Set admin data_limit / max_users from local SoT totals; 0 leaves that limit as it is."""
    uname = normalize_pg_username(username)
    if not uname:
        return
    current: dict[str, Any] | None = None
    if total_users > 0:
        admin = await pg.get_admin(uname)
        if isinstance(admin, dict) and isinstance(admin.get("permission_overrides"), dict):
            current = admin["permission_overrides"]
    payload = _capacity_payload(total_gb, total_users, current)
    if not payload:
        return
    await pg.modify_admin(uname, payload)


async def assert_new_admin_fits_owner_budget(session: AsyncSession, plan: ResellerPlan, pg_username: str) -> None:
    """Refuse a package for a panel admin about to be created when the owner's panel budget is full."""
    if not is_subscription_plan(plan) or is_current_capacity_plan(plan):
        return
    await _assert_owner_capacity_budget(
        session,
        target_pg_username=pg_username,
        next_total_gb=max(0, int(getattr(plan, "included_gb", 0) or 0)),
        next_total_users=max(0, int(getattr(plan, "included_users", 0) or 0)),
        previous_total_gb=0,
        previous_total_users=0,
    )


async def _assert_owner_capacity_budget(
    session: AsyncSession,
    *,
    target_pg_username: str,
    next_total_gb: int,
    next_total_users: int,
    previous_total_gb: int,
    previous_total_users: int,
) -> None:
    """Do not oversell child-admin capacity beyond the installer's live PG budget.

    ``previous_total_*`` must be the caller's pre-change totals (before any flush). Growth
    detection must not re-read the row after ``record_unit_extra`` has already written the
    new extras — that would treat growth as "same capacity" and skip the fail-closed path.

    If the owner admin cannot be read: refuse only when this call **grows** capacity.
    Same-or-smaller totals (typical renew) still proceed.
    """
    from app.config import get_settings
    from app.services.pg_quota import _as_int, merge_role_limits

    owner_username = normalize_pg_username(getattr(get_settings(), "pg_username", None))
    if not owner_username or normalize_pg_username(target_pg_username) == owner_username:
        return

    from app.services.pasarguard import get_pg

    growing = int(next_total_gb) > int(previous_total_gb) or int(next_total_users) > int(
        previous_total_users
    )

    pg = get_pg()
    try:
        admin = await pg.get_admin(owner_username)
    except Exception as exc:
        logger.warning(
            "owner capacity budget: install admin unreadable (%s)", owner_username, exc_info=True,
        )
        if growing:
            raise ValueError(
                "سقف ظرفیت ادمین اصلی خوانده نشد؛ کمی بعد دوباره تلاش کنید"
            ) from exc
        return
    if not isinstance(admin, dict) or not admin:
        if growing:
            raise ValueError("سقف ظرفیت ادمین اصلی خوانده نشد؛ کمی بعد دوباره تلاش کنید")
        return
    role = admin.get("role") if isinstance(admin.get("role"), dict) else None
    limits = merge_role_limits(admin, role)
    owner_gb_limit_bytes = max(
        0, _as_int(limits.get("data_limit")) or _as_int(admin.get("data_limit")) or 0
    )
    owner_gb_limit = int(owner_gb_limit_bytes / GB) if owner_gb_limit_bytes > 0 else 0
    owner_user_limit = max(
        0, _as_int(limits.get("max_users")) or _as_int(admin.get("max_users")) or 0
    )

    rows = list(
        (
            await session.execute(
                select(PgAdminSubscription).where(
                    PgAdminSubscription.access_status == STATUS_ACTIVE
                )
            )
        ).scalars().all()
    )
    target = normalize_pg_username(target_pg_username)
    sold_gb = 0
    sold_users = 0
    if target not in {normalize_pg_username(getattr(row, "pg_username", None)) for row in rows}:
        # An admin about to be created has no subscription row yet; it still sells its capacity.
        sold_gb += max(0, int(next_total_gb))
        sold_users += max(0, int(next_total_users))
    for row in rows:
        uname = normalize_pg_username(getattr(row, "pg_username", None))
        if not uname or uname == owner_username:
            continue
        total_gb = int(getattr(row, "base_gb", 0) or 0) + int(
            getattr(row, "extra_gb_purchased", 0) or 0
        )
        total_users = int(getattr(row, "base_users", 0) or 0) + int(
            getattr(row, "extra_users_purchased", 0) or 0
        )
        if uname == target:
            sold_gb += max(0, int(next_total_gb))
            sold_users += max(0, int(next_total_users))
        else:
            sold_gb += max(0, total_gb)
            sold_users += max(0, total_users)

    if owner_gb_limit > 0 and sold_gb > owner_gb_limit:
        raise ValueError(
            f"ظرفیت فروخته‌شده نمایندگی از سقف حجم ادمین اصلی بیشتر می‌شود ({sold_gb} از {owner_gb_limit} گیگ)"
        )
    if owner_user_limit > 0 and sold_users > owner_user_limit:
        raise ValueError(
            f"ظرفیت فروخته‌شده نمایندگی از سقف کاربر ادمین اصلی بیشتر می‌شود ({sold_users} از {owner_user_limit})"
        )


async def sync_pg_capacity_from_sub(session: AsyncSession, sub: PgAdminSubscription) -> None:
    from app.services.pasarguard import get_pg

    total_gb = max(0, int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0))
    total_users = max(0, int(sub.base_users or 0) + int(sub.extra_users_purchased or 0))
    try:
        await _apply_pg_capacity(
            get_pg(), sub.pg_username, total_gb=total_gb, total_users=total_users
        )
    except Exception:
        logger.exception("sync PG capacity failed admin=%s", sub.pg_username)
        raise


async def start_or_refresh_subscription(
    session: AsyncSession,
    *,
    pg_username: str,
    plan: ResellerPlan,
    reset_extras: bool = False,
    apply_pg_limits: bool = True,
) -> PgAdminSubscription:
    """Bind/refresh subscription from a clock-bearing plan (not addons).

    ``current_capacity`` snapshots live PG limits into ``base_*`` and never
    overwrites PG with the plan's zero ``included_*`` fields.
    """
    if not is_subscription_plan(plan):
        raise ValueError("این پلن از نوع اشتراک نیست")
    preserve = is_current_capacity_plan(plan)

    async def _resolve_base() -> tuple[int, int]:
        if not preserve:
            return (
                max(0, int(getattr(plan, "included_gb", 0) or 0)),
                max(0, int(getattr(plan, "included_users", 0) or 0)),
            )
        try:
            return await read_live_pg_admin_capacity(pg_username)
        except PgCapacityUnreadable:
            # A refresh may keep what is already recorded; a first grant must not guess.
            existing = await get_subscription(session, pg_username)
            if existing is not None:
                return (
                    max(0, int(existing.base_gb or 0)),
                    max(0, int(existing.base_users or 0)),
                )
            raise

    # PAYG: no time clock (current_capacity is always fixed billing)
    if str(getattr(plan, "billing_mode", "") or "").lower() == "payg" and not preserve:
        sub = await get_or_create_subscription(session, pg_username)
        previous_gb = _total_gb(sub)
        previous_users = _total_users(sub)
        sub.plan_id = int(plan.id)
        sub.access_status = STATUS_ACTIVE
        sub.expires_at = None
        sub.base_gb = max(0, int(getattr(plan, "included_gb", 0) or 0))
        sub.base_users = max(0, int(getattr(plan, "included_users", 0) or 0))
        if reset_extras:
            sub.extra_gb_purchased = 0
            sub.extra_users_purchased = 0
        sub.started_at = sub.started_at or _utcnow()
        await session.flush()
        await assert_parent_has_room(session, pg_username=sub.pg_username, previous_total_gb=previous_gb)
        if apply_pg_limits and (sub.base_gb or sub.base_users):
            await sync_pg_capacity_from_sub(session, sub)
        return sub

    base_gb, base_users = await _resolve_base()
    sub = await get_or_create_subscription(session, pg_username)
    previous_gb = _total_gb(sub)
    previous_users = _total_users(sub)
    now = _utcnow()
    days = max(0, int(getattr(plan, "duration_days", 0) or 0))
    sub.plan_id = int(plan.id)
    sub.access_status = STATUS_ACTIVE
    sub.started_at = sub.started_at or now
    sub.base_gb = base_gb
    sub.base_users = base_users
    if reset_extras:
        sub.extra_gb_purchased = 0
        sub.extra_users_purchased = 0
    if days > 0:
        sub.expires_at = now + timedelta(days=days)
    else:
        sub.expires_at = None
    sub.expired_at = None
    sub.expiry_disabled_user_ids = None
    sub.warn_sent_at = None
    await session.flush()
    # A current_capacity grant records what the admin already has; it does not sell anything new.
    if not preserve:
        await assert_parent_has_room(session, pg_username=sub.pg_username, previous_total_gb=previous_gb)
        await _assert_owner_capacity_budget(
            session,
            target_pg_username=sub.pg_username,
            next_total_gb=int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0),
            next_total_users=int(sub.base_users or 0) + int(sub.extra_users_purchased or 0),
            previous_total_gb=previous_gb,
            previous_total_users=previous_users,
        )
    # current_capacity: SoT mirrors live PG — sync only when extras need push,
    # never when totals are zero (would clear unlimited / unread limits).
    if apply_pg_limits and (
        sub.extra_gb_purchased
        or sub.extra_users_purchased
        or (not preserve and (sub.base_gb or sub.base_users))
    ):
        await sync_pg_capacity_from_sub(session, sub)
    return sub


async def _set_web_active_for_pg_admin(
    session: AsyncSession, pg_username: str, *, active: bool
) -> None:
    uname = normalize_pg_username(pg_username)
    if not uname:
        return
    await session.execute(
        update(ResellerProfile)
        .where(ResellerProfile.pg_admin_username == uname)
        .values(is_active=active)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(PgStaffAccess)
        .where(PgStaffAccess.pg_username == uname)
        .values(is_active=active)
        .execution_options(synchronize_session=False)
    )


async def _expire_claim_still_held(session: AsyncSession, sub_id: int, claimed_expired_at: datetime) -> bool:
    """True when this expire claim still owns the row (a concurrent renew clears ``expired_at``)."""
    row = await session.get(PgAdminSubscription, int(sub_id))
    if row is None or row.access_status != STATUS_EXPIRED or row.expired_at is None:
        return False
    left = _as_aware(row.expired_at)
    right = _as_aware(claimed_expired_at)
    return left is not None and right is not None and left == right


async def expire_subscription(
    session: AsyncSession,
    sub: PgAdminSubscription,
    *,
    commit: bool = True,
) -> dict[str, int]:
    """Disable web + PG admin + cut owned users.

    Claims the due active row atomically (``ACTIVE`` + ``expires_at <= now`` → ``EXPIRED``)
    and commits that claim before any panel call, so a concurrent renew that extends
    ``expires_at`` wins the race. If renew takes the row back mid-panel, expiry aborts and
    restores access instead of leaving a paid period cut.
    """
    from app.services.billing_suspend import _set_admin_enabled, list_owned_user_ids
    from app.services.pasarguard import get_pg

    stats = {"users_cut": 0, "already": 0, "errors": 0, "skipped": 0, "aborted": 0}
    if sub.access_status == STATUS_REVOKED:
        return stats
    if sub.access_status == STATUS_EXPIRED and sub.expired_at:
        stats["already"] = 1
        await _set_web_active_for_pg_admin(session, sub.pg_username, active=False)
        if commit:
            await session.commit()
        return stats

    now = _utcnow()
    sub_id = int(sub.id)
    claimed = await session.execute(
        update(PgAdminSubscription)
        .where(
            PgAdminSubscription.id == sub_id,
            PgAdminSubscription.access_status == STATUS_ACTIVE,
            PgAdminSubscription.expires_at.is_not(None),
            PgAdminSubscription.expires_at <= now,
        )
        .values(access_status=STATUS_EXPIRED, expired_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        # Renew extended the clock, or another worker claimed first.
        await session.refresh(sub)
        stats["skipped"] = 1
        if commit:
            await session.commit()
        return stats

    # Publish the claim before any network call (renew reads the same row).
    await session.commit()
    await session.refresh(sub)
    claimed_at = sub.expired_at
    if claimed_at is None:
        stats["skipped"] = 1
        return stats

    uname = normalize_pg_username(sub.pg_username)
    pg = get_pg()
    cut_ids: list[int] = []

    async def _abort_if_renewed() -> bool:
        if await _expire_claim_still_held(session, sub_id, claimed_at):
            return False
        stats["aborted"] = 1
        try:
            # Include users cut in this run: they are not yet on expiry_disabled_user_ids.
            await restore_subscription_access(
                session, sub, commit=False, also_enable_user_ids=cut_ids,
            )
            await session.commit()
        except Exception:
            logger.exception("expire: restore after renew race failed admin=%s", uname)
            stats["errors"] += 1
        return True

    if await _abort_if_renewed():
        return stats

    try:
        await _set_admin_enabled(pg, uname, enabled=False)
    except Exception:
        logger.exception("expire: disable admin failed %s", uname)
        stats["errors"] += 1

    if await _abort_if_renewed():
        return stats

    try:
        owned = await list_owned_user_ids(pg, uname)
    except Exception:
        logger.exception("expire: list users failed %s", uname)
        owned = []
        stats["errors"] += 1

    for uid in owned:
        if not await _expire_claim_still_held(session, sub_id, claimed_at):
            stats["aborted"] = 1
            try:
                await restore_subscription_access(
                    session, sub, commit=False, also_enable_user_ids=cut_ids,
                )
                await session.commit()
            except Exception:
                logger.exception("expire: restore after renew race failed admin=%s", uname)
                stats["errors"] += 1
            return stats
        try:
            await pg.set_disabled_by_id(uid, True)
            cut_ids.append(uid)
            stats["users_cut"] += 1
        except Exception:
            stats["errors"] += 1
            logger.debug("expire cut user %s failed", uid, exc_info=True)

    if await _abort_if_renewed():
        return stats

    await session.refresh(sub)
    existing = _parse_id_list(sub.expiry_disabled_user_ids)
    merged = sorted(set(existing) | set(cut_ids))
    sub.expiry_disabled_user_ids = _dump_id_list(merged) if merged else None
    await _set_web_active_for_pg_admin(session, uname, active=False)
    await session.flush()
    if commit:
        await session.commit()
    return stats


async def restore_subscription_access(
    session: AsyncSession,
    sub: PgAdminSubscription,
    *,
    commit: bool = False,
    also_enable_user_ids: list[int] | None = None,
) -> dict[str, int]:
    """Re-enable PG admin + previously cut users + web logins.

    ``also_enable_user_ids`` covers users disabled in the current expire pass before they
    were written to ``expiry_disabled_user_ids`` (renew race mid-cut).
    """
    from app.services.billing_suspend import _set_admin_enabled
    from app.services.pasarguard import get_pg

    stats = {"users_restored": 0, "errors": 0}
    if sub.access_status == STATUS_REVOKED:
        raise ValueError("این اشتراک لغو شده و با تمدید باز نمی‌شود")

    uname = normalize_pg_username(sub.pg_username)
    pg = get_pg()
    try:
        await _set_admin_enabled(pg, uname, enabled=True)
    except Exception:
        logger.exception("restore: enable admin failed %s", uname)
        stats["errors"] += 1

    restore_ids = set(_parse_id_list(sub.expiry_disabled_user_ids))
    for uid in also_enable_user_ids or []:
        try:
            restore_ids.add(int(uid))
        except (TypeError, ValueError):
            continue
    for uid in sorted(restore_ids):
        try:
            await pg.set_disabled_by_id(uid, False)
            stats["users_restored"] += 1
        except Exception:
            stats["errors"] += 1
            logger.debug("restore user %s failed", uid, exc_info=True)

    sub.access_status = STATUS_ACTIVE
    sub.expired_at = None
    sub.expiry_disabled_user_ids = None
    await _set_web_active_for_pg_admin(session, uname, active=True)
    await session.flush()
    if commit:
        await session.commit()
    return stats


RENEW_REFUSED = "پاسارگارد تمدید را نپذیرفت — مبلغ بازگردانده شد و چیزی تغییر نکرد"
RENEW_UNKNOWN_WARNING = (
    "پاسخ پاسارگارد برای اعمال تمدید نرسید. تمدید ثبت شده؛ دوباره تمدید نکنید. "
    "اگر ظرفیت یا مصرف درست نشد با پشتیبانی تماس بگیرید."
)
NO_TIME_RENEWAL = "این پلن زمان ندارد و تمدید معنی ندارد؛ برای افزایش حجم یا کاربر بسته بخرید یا پلن را ارتقا دهید"


def plan_has_time(plan: ResellerPlan | None) -> bool:
    """True when the plan sells a period; only such plans are renewed and refill the admin's volume."""
    return plan is not None and int(getattr(plan, "duration_days", 0) or 0) > 0


@dataclass(slots=True)
class _RenewSnapshot:
    """The subscription fields a renewal rewrites, kept so a refused renewal can put them back."""

    plan_id: int | None
    expires_at: datetime | None
    base_gb: int
    base_users: int
    extra_gb_purchased: int
    extra_users_purchased: int
    last_renewed_at: datetime | None
    warn_sent_at: datetime | None
    access_status: str
    expired_at: datetime | None

    @classmethod
    def of(cls, sub: PgAdminSubscription) -> _RenewSnapshot:
        return cls(
            sub.plan_id,
            sub.expires_at,
            int(sub.base_gb or 0),
            int(sub.base_users or 0),
            int(sub.extra_gb_purchased or 0),
            int(sub.extra_users_purchased or 0),
            sub.last_renewed_at,
            sub.warn_sent_at,
            sub.access_status,
            sub.expired_at,
        )

    def restore(self, sub: PgAdminSubscription) -> None:
        for name, value in asdict(self).items():
            setattr(sub, name, value)


class _RenewRefused(Exception):
    """The panel definitely refused a renewal step, so nothing it sold was applied."""


def _extend_period(sub: PgAdminSubscription, plan: ResellerPlan, now: datetime) -> None:
    days = max(0, int(getattr(plan, "duration_days", 0) or 0))
    if days > 0:
        anchor = _as_aware(sub.expires_at) or now
        if (not plan_carries_remaining_period(plan)) or anchor < now:
            anchor = now
        sub.expires_at = anchor + timedelta(days=days)
    else:
        # Moving to an untimed package must not keep a stale clock that the expiry tick can cut.
        sub.expires_at = None
    sub.plan_id = int(plan.id)
    # current_capacity keeps the snapshot of the admin's live limits; its zero included_* never overwrite it.
    if not is_current_capacity_plan(plan):
        sub.base_gb = max(0, int(getattr(plan, "included_gb", 0) or 0))
        sub.base_users = max(0, int(getattr(plan, "included_users", 0) or 0))
    if not plan_keeps_extras_on_renew(plan):
        sub.extra_gb_purchased = 0
        sub.extra_users_purchased = 0
    sub.last_renewed_at = now
    sub.warn_sent_at = None
    # Paid means active; the panel is reopened right after, and a failure there only warns.
    sub.access_status = STATUS_ACTIVE
    sub.expired_at = None


async def _panel_step(name: str, uname: str, step, warnings: list[str]) -> bool:
    """Run one panel call of a renewal: True if done, a refusal aborts it, an unanswered call warns."""
    try:
        await step()
    except Exception as exc:
        if is_hard_panel_rejection(exc):
            logger.warning("renewal step %s refused admin=%s", name, uname, exc_info=True)
            raise _RenewRefused from exc
        logger.error("renewal step %s outcome unknown admin=%s", name, uname, exc_info=True)
        if RENEW_UNKNOWN_WARNING not in warnings:
            warnings.append(RENEW_UNKNOWN_WARNING)
        return False
    return True


def _has_capacity_totals(sub: PgAdminSubscription) -> bool:
    """False when every total is zero (unlimited or unread): pushing then would clear live limits."""
    return any(
        int(value or 0)
        for value in (sub.base_gb, sub.base_users, sub.extra_gb_purchased, sub.extra_users_purchased)
    )


@dataclass(slots=True)
class _PanelOutcome:
    restored: dict[str, int]
    usage_reset: bool


async def _apply_renewal_on_panel(
    session: AsyncSession, sub: PgAdminSubscription, warnings: list[str], *, refills_volume: bool,
) -> _PanelOutcome:
    """Refill the admin's volume (a new period only), push its limits and reopen access.

    A customer's usage is never touched; only the admin's own counter is reset.
    """
    from app.services.pasarguard import get_pg

    uname = normalize_pg_username(sub.pg_username)
    usage_reset = refills_volume and await _panel_step(
        "reset_usage", uname, lambda: get_pg().reset_admin_usage(uname), warnings,
    )
    if _has_capacity_totals(sub):
        await _panel_step("capacity", uname, lambda: sync_pg_capacity_from_sub(session, sub), warnings)
    restored = await restore_subscription_access(session, sub, commit=False)
    if restored["errors"]:
        warnings.append("بازگرداندن دسترسی ادمین یا کاربران کامل انجام نشد؛ در صورت مشکل با پشتیبانی تماس بگیرید.")
    return _PanelOutcome(restored=restored, usage_reset=usage_reset)


async def _undo_refused_renewal(
    session: AsyncSession,
    sub: PgAdminSubscription,
    snapshot: _RenewSnapshot,
    *,
    payer: BotUser | None,
    amount: int,
) -> None:
    """Put the subscription back and return the money; the generation stays spent so the old page is stale."""
    await rollback_quiet(session)
    fresh = await session.get(PgAdminSubscription, int(sub.id))
    if fresh is not None:
        snapshot.restore(fresh)
    if payer is not None and amount > 0:
        from app.services.wallet import credit_wallet

        await credit_wallet(session, payer, amount, reason=f"reseller_renew_refund:{sub.pg_username}", commit=False)
    await session.commit()


async def _claim_renewal(session: AsyncSession, sub: PgAdminSubscription) -> None:
    """Atomically take the generation the buyer saw; a concurrent or repeated renewal loses."""
    claimed = await session.execute(
        update(PgAdminSubscription)
        .where(
            PgAdminSubscription.id == int(sub.id),
            PgAdminSubscription.renew_generation == int(sub.renew_generation or 0),
            PgAdminSubscription.access_status.in_([STATUS_ACTIVE, STATUS_EXPIRED]),
        )
        .values(renew_generation=PgAdminSubscription.renew_generation + 1)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ValueError("تمدید هم‌زمان در حال انجام است — کمی بعد دوباره تلاش کنید")
    await session.refresh(sub)


async def renew_subscription(
    session: AsyncSession,
    *,
    sub: PgAdminSubscription,
    plan: ResellerPlan,
    payer: BotUser | None,
    charge_wallet: bool = True,
    moving: bool = False,
) -> dict[str, Any]:
    """Start the plan's next period: extend the time, refill the admin's volume, reopen access.

    Early renewal extends from max(now, expires_at). The amount is recomputed here. The charge
    and the new period are committed before the panel is called. If the panel refuses a step the
    renewal is undone and the money returned; if it does not answer, the renewal stands and the
    result carries a warning (no blind refund). A customer's usage is never reset. A plan with no
    period cannot be renewed: it changes only through a pack or by moving to it (``moving``), and
    moving to it neither extends the time nor refills the volume.
    """
    from app.services.wallet import debit_wallet

    if not is_subscription_plan(plan):
        raise ValueError("تمدید فقط برای پلن اشتراک است")
    if sub.access_status == STATUS_REVOKED:
        raise ValueError("اشتراک لغو شده است")
    if str(getattr(plan, "billing_mode", "") or "").lower() == "payg":
        raise ValueError("پلن PAYG تمدید زمانی ندارد")
    timed = plan_has_time(plan)
    if not timed and not moving:
        raise ValueError(NO_TIME_RENEWAL)
    if charge_wallet and payer is None:
        raise ValueError("پرداخت‌کننده مشخص نیست")

    await _claim_renewal(session, sub)
    if not is_current_capacity_plan(plan):
        # Checked before the charge; the parent stays locked until this renewal commits.
        await assert_child_growth_fits(
            session, pg_username=sub.pg_username,
            add_gb=max(0, int(getattr(plan, "included_gb", 0) or 0)) - int(sub.base_gb or 0),
        )
    invoice = compute_renew_invoice(plan, sub)
    amount = int(invoice["total"]) if charge_wallet else 0
    if charge_wallet:
        await debit_wallet(
            session, payer, amount, reason=f"reseller_renew:{sub.pg_username}:g{sub.renew_generation}", commit=False,
        )
    snapshot = _RenewSnapshot.of(sub)
    _extend_period(sub, plan, _utcnow())
    await session.commit()

    warnings: list[str] = []
    try:
        outcome = await _apply_renewal_on_panel(session, sub, warnings, refills_volume=timed)
    except _RenewRefused:
        await _undo_refused_renewal(session, sub, snapshot, payer=payer, amount=amount)
        raise ValueError(RENEW_REFUSED) from None
    await session.commit()
    return {
        "amount": amount, "invoice": invoice, "expires_at": sub.expires_at, "restore": outcome.restored,
        "timed": timed, "usage_reset": outcome.usage_reset, "warnings": warnings,
    }


async def record_unit_extra(
    session: AsyncSession,
    *,
    pg_username: str,
    gb: int = 0,
    users: int = 0,
) -> PgAdminSubscription | None:
    """Track quick-buy extras (1/5/10…) on the shared subscription SoT."""
    uname = normalize_pg_username(pg_username)
    if not uname:
        return None
    sub = await get_or_create_subscription(session, uname)
    previous_gb = _total_gb(sub)
    previous_users = _total_users(sub)
    if gb:
        sub.extra_gb_purchased = int(sub.extra_gb_purchased or 0) + max(0, int(gb))
    if users:
        sub.extra_users_purchased = int(sub.extra_users_purchased or 0) + max(0, int(users))
    await session.flush()
    await assert_parent_has_room(session, pg_username=sub.pg_username, previous_total_gb=previous_gb)
    await _assert_owner_capacity_budget(
        session,
        target_pg_username=sub.pg_username,
        next_total_gb=int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0),
        next_total_users=int(sub.base_users or 0) + int(sub.extra_users_purchased or 0),
        previous_total_gb=previous_gb,
        previous_total_users=previous_users,
    )
    return sub


EXPIRY_TICK_LIMIT = 100


async def run_subscription_expiry_tick(session: AsyncSession) -> dict[str, int]:
    """Scheduler: expire due subscriptions; skip PAYG-linked plans.

    Processes at most ``EXPIRY_TICK_LIMIT`` due rows per tick. Each expiry claims and
    commits on its own so a renew cannot be overwritten by a stale in-memory row.
    """
    now = _utcnow()
    result = await session.execute(
        select(PgAdminSubscription)
        .where(
            PgAdminSubscription.access_status == STATUS_ACTIVE,
            PgAdminSubscription.expires_at.is_not(None),
            PgAdminSubscription.expires_at <= now,
        )
        .order_by(PgAdminSubscription.id)
        .limit(EXPIRY_TICK_LIMIT)
    )
    due = list(result.scalars().all())
    stats = {
        "expired": 0, "skipped_payg": 0, "skipped": 0, "aborted": 0, "errors": 0, "warned": 0,
    }

    # Warnings (active, within WARN_DAYS_BEFORE)
    warn_before = now + timedelta(days=WARN_DAYS_BEFORE)
    warn_q = await session.execute(
        select(PgAdminSubscription)
        .where(
            PgAdminSubscription.access_status == STATUS_ACTIVE,
            PgAdminSubscription.expires_at.is_not(None),
            PgAdminSubscription.expires_at > now,
            PgAdminSubscription.expires_at <= warn_before,
            PgAdminSubscription.warn_sent_at.is_(None),
        )
        .order_by(PgAdminSubscription.id)
        .limit(EXPIRY_TICK_LIMIT)
    )
    for sub in warn_q.scalars().all():
        sub.warn_sent_at = now
        stats["warned"] += 1
        try:
            await _notify_expiry_warning(session, sub)
        except Exception:
            logger.debug("expiry warn notify failed %s", sub.pg_username, exc_info=True)
    await session.commit()

    for sub in due:
        plan = None
        if sub.plan_id:
            plan = await session.get(ResellerPlan, int(sub.plan_id))
        if plan is not None and str(getattr(plan, "billing_mode", "") or "").lower() == "payg":
            stats["skipped_payg"] += 1
            continue
        rp = (
            await session.execute(
                select(ResellerProfile).where(
                    ResellerProfile.pg_admin_username == sub.pg_username
                )
            )
        ).scalar_one_or_none()
        if rp is not None and is_payg(rp):
            stats["skipped_payg"] += 1
            continue
        try:
            # Expire commits its own claim; never batch-commit over a renewed row.
            outcome = await expire_subscription(session, sub, commit=True)
            if outcome.get("skipped"):
                stats["skipped"] += 1
            elif outcome.get("aborted"):
                stats["aborted"] += 1
            elif outcome.get("already"):
                stats["expired"] += 1
            else:
                stats["expired"] += 1
                try:
                    await _notify_expired(session, sub)
                except Exception:
                    logger.debug("expired notify failed %s", sub.pg_username, exc_info=True)
        except Exception:
            stats["errors"] += 1
            logger.exception("expire tick failed admin=%s", sub.pg_username)

    return stats


async def _notify_expiry_warning(session: AsyncSession, sub: PgAdminSubscription) -> None:
    # Soft: mark only; bot delivery can be added via notifications service later
    _ = session
    logger.info(
        "subscription expiry warning admin=%s expires_at=%s",
        sub.pg_username,
        sub.expires_at,
    )


async def _notify_expired(session: AsyncSession, sub: PgAdminSubscription) -> None:
    _ = session
    logger.info("subscription expired admin=%s", sub.pg_username)


ADDON_LIST_LIMIT = 60


def _prefer_category_addons(packs: list[ResellerPlan]) -> list[ResellerPlan]:
    """Drop a general pack when a categorised pack offers the same kind and amount; keep the order."""

    def bundle(pack: ResellerPlan) -> tuple[str, int]:
        kind = plan_kind_of(pack)
        return kind, int(pack.addon_gb or 0) if kind == PLAN_KIND_ADDON_VOLUME else int(pack.addon_users or 0)

    specific = {bundle(pack) for pack in packs if pack.category_id is not None}
    return [pack for pack in packs if pack.category_id is not None or bundle(pack) not in specific]


async def list_addon_plans(
    session: AsyncSession,
    *,
    kind: str | None = None,
    category_id: int | None | object = ...,
) -> list[ResellerPlan]:
    """Active reseller capacity packs.

    Omit ``category_id`` for the full catalog (admin lists). Pass the subscription-plan category
    to get what a plan of that category may buy: its own packs plus the packs without a category,
    which fit every category (``None`` means a plan without a category: general packs only). A
    general pack is hidden when a pack of the category sells the same bundle.
    """
    kinds = [kind] if kind in {PLAN_KIND_ADDON_VOLUME, PLAN_KIND_ADDON_USERS} else [
        PLAN_KIND_ADDON_VOLUME, PLAN_KIND_ADDON_USERS,
    ]
    q = select(ResellerPlan).where(ResellerPlan.is_active.is_(True), ResellerPlan.plan_kind.in_(kinds))
    if category_id is not ...:
        fits = ResellerPlan.category_id.is_(None)
        if category_id is not None:
            fits = or_(fits, ResellerPlan.category_id == int(category_id))
        q = q.where(fits)
    q = q.order_by(ResellerPlan.sort_order, ResellerPlan.id).limit(ADDON_LIST_LIMIT)
    packs = list((await session.execute(q)).scalars().all())
    return _prefer_category_addons(packs) if category_id is not ... else packs


async def list_subscription_plans(session: AsyncSession) -> list[ResellerPlan]:
    """Active clock-bearing plans (sellable subscriptions only — not current_capacity)."""
    q = (
        select(ResellerPlan)
        .where(
            ResellerPlan.is_active.is_(True),
            ResellerPlan.plan_kind == PLAN_KIND_SUBSCRIPTION,
        )
        .order_by(ResellerPlan.sort_order, ResellerPlan.id)
    )
    return list((await session.execute(q)).scalars().all())


# Same caps as user-service extend (admin free adjust).
MAX_ADJUST_DAYS = 3650
MAX_ADJUST_GB = 10_000


async def admin_adjust_reseller_subscription(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    extra_days: int = 0,
    extra_gb: int = 0,
) -> dict[str, Any]:
    """Owner-admin free adjust of reseller PG subscription days/GB (signed deltas).

    Not the paid buy_extra path. Fail-closed without pg username / subscription row.
    """
    days_n = int(extra_days or 0)
    gb_n = int(extra_gb or 0)
    if days_n == 0 and gb_n == 0:
        raise ValueError("حداقل یک مقدار غیرصفر وارد کنید")
    if abs(days_n) > MAX_ADJUST_DAYS:
        raise ValueError("تعداد روز نامعتبر است")
    if abs(gb_n) > MAX_ADJUST_GB:
        raise ValueError("حجم نامعتبر است")

    uname = (getattr(profile, "pg_admin_username", None) or "").strip()
    if not uname:
        raise ValueError("ادمین پاسارگارد برای این نماینده تنظیم نشده است")

    sub = await get_subscription(session, uname)
    if sub is None:
        raise ValueError("اشتراک پاسارگارد برای این نماینده یافت نشد")

    now = _utcnow()
    if days_n != 0:
        cur_exp = _as_aware(sub.expires_at)
        if cur_exp is None:
            if days_n < 0:
                raise ValueError("اشتراک بدون انقضا است — کاهش روز ممکن نیست")
            sub.expires_at = now + timedelta(days=days_n)
        else:
            sub.expires_at = cur_exp + timedelta(days=days_n)

    previous_gb = _total_gb(sub)
    previous_users = _total_users(sub)
    if gb_n != 0:
        base = max(0, int(sub.base_gb or 0))
        extra = max(0, int(sub.extra_gb_purchased or 0))
        if gb_n > 0:
            sub.extra_gb_purchased = extra + gb_n
        else:
            remain = -gb_n
            take_extra = min(extra, remain)
            sub.extra_gb_purchased = extra - take_extra
            remain -= take_extra
            if remain:
                sub.base_gb = max(0, base - remain)

    await session.flush()
    await assert_parent_has_room(session, pg_username=sub.pg_username, previous_total_gb=previous_gb)
    await _assert_owner_capacity_budget(
        session,
        target_pg_username=sub.pg_username,
        next_total_gb=int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0),
        next_total_users=int(sub.base_users or 0) + int(sub.extra_users_purchased or 0),
        previous_total_gb=previous_gb,
        previous_total_users=previous_users,
    )
    try:
        await sync_pg_capacity_from_sub(session, sub)
    except Exception:
        logger.exception("admin adjust sync PG failed admin=%s", uname)
        raise ValueError("همگام‌سازی ظرفیت پاسارگارد ناموفق بود") from None

    await session.commit()
    return {
        "expires_at": sub.expires_at,
        "base_gb": int(sub.base_gb or 0),
        "extra_gb": int(sub.extra_gb_purchased or 0),
        "total_gb": int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0),
        "pg_username": sub.pg_username,
    }
