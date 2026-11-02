"""Shared PG-admin subscription: time expiry, capacity extras, renew invoice.

Clock is keyed by PasarGuard username so reseller and pg_staff share one period.
PAYG profiles are skipped (no time gate). Mid-period add-ons do not extend expires_at.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    PgAdminSubscription,
    PgStaffAccess,
    ResellerPlan,
    ResellerProfile,
)
from app.services.billing import GB, is_payg
from app.services.formatting import format_toman
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


async def get_or_create_subscription(
    session: AsyncSession, pg_username: str
) -> PgAdminSubscription:
    uname = normalize_pg_username(pg_username)
    if not uname:
        raise ValueError("نام ادمین پاسارگارد نامعتبر است")
    sub = await get_subscription(session, uname)
    if sub:
        return sub
    sub = PgAdminSubscription(pg_username=uname, access_status=STATUS_ACTIVE)
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
    """
    base = renew_base_amount(plan)
    extra_gb = int(getattr(sub, "extra_gb_purchased", 0) or 0) if sub else 0
    extra_users = int(getattr(sub, "extra_users_purchased", 0) or 0) if sub else 0
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
    )


async def _assert_owner_capacity_budget(
    session: AsyncSession,
    *,
    target_pg_username: str,
    next_total_gb: int,
    next_total_users: int,
) -> None:
    """Do not oversell child-admin capacity beyond the installer's live PG budget."""
    from app.config import get_settings
    from app.services.pg_quota import _as_int, merge_role_limits

    owner_username = normalize_pg_username(getattr(get_settings(), "pg_username", None))
    if not owner_username or normalize_pg_username(target_pg_username) == owner_username:
        return

    from app.services.pasarguard import get_pg

    pg = get_pg()
    admin = await pg.get_admin(owner_username)
    if not isinstance(admin, dict) or not admin:
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
    sold_gb = 0
    sold_users = 0
    target = normalize_pg_username(target_pg_username)
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


async def expire_subscription(
    session: AsyncSession,
    sub: PgAdminSubscription,
    *,
    commit: bool = True,
) -> dict[str, int]:
    """Disable web + PG admin + cut owned users. Idempotent when already expired."""
    from app.services.billing_suspend import _set_admin_enabled, list_owned_user_ids
    from app.services.pasarguard import get_pg

    stats = {"users_cut": 0, "already": 0, "errors": 0}
    if sub.access_status == STATUS_REVOKED:
        return stats
    if sub.access_status == STATUS_EXPIRED and sub.expired_at:
        stats["already"] = 1
        # Still ensure web is off
        await _set_web_active_for_pg_admin(session, sub.pg_username, active=False)
        if commit:
            await session.commit()
        return stats

    uname = normalize_pg_username(sub.pg_username)
    pg = get_pg()
    cut_ids: list[int] = []
    try:
        await _set_admin_enabled(pg, uname, enabled=False)
    except Exception:
        logger.exception("expire: disable admin failed %s", uname)
        stats["errors"] += 1

    try:
        owned = await list_owned_user_ids(pg, uname)
    except Exception:
        logger.exception("expire: list users failed %s", uname)
        owned = []
        stats["errors"] += 1

    for uid in owned:
        try:
            await pg.set_disabled_by_id(uid, True)
            cut_ids.append(uid)
            stats["users_cut"] += 1
        except Exception:
            stats["errors"] += 1
            logger.debug("expire cut user %s failed", uid, exc_info=True)

    existing = _parse_id_list(sub.expiry_disabled_user_ids)
    merged = sorted(set(existing) | set(cut_ids))
    sub.expiry_disabled_user_ids = _dump_id_list(merged) if merged else None
    sub.access_status = STATUS_EXPIRED
    sub.expired_at = _utcnow()
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
) -> dict[str, int]:
    """Re-enable PG admin + previously cut users + web logins."""
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

    for uid in _parse_id_list(sub.expiry_disabled_user_ids):
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


async def renew_subscription(
    session: AsyncSession,
    *,
    sub: PgAdminSubscription,
    plan: ResellerPlan,
    payer: BotUser | None,
    charge_wallet: bool = True,
    reset_user_traffic: bool = True,
) -> dict[str, Any]:
    """Extend period, restore access, optionally charge wallet + reset traffic.

    Early renew extends from max(now, expires_at). Amount is recomputed server-side.
    """
    if not is_subscription_plan(plan):
        raise ValueError("تمدید فقط برای پلن اشتراک است")
    if sub.access_status == STATUS_REVOKED:
        raise ValueError("اشتراک لغو شده است")
    if str(getattr(plan, "billing_mode", "") or "").lower() == "payg":
        raise ValueError("پلن PAYG تمدید زمانی ندارد")

    # Claim generation for idempotency under concurrent clicks
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
    if not is_current_capacity_plan(plan):
        # Checked before the charge; the parent stays locked until this renewal commits.
        await assert_child_growth_fits(
            session, pg_username=sub.pg_username,
            add_gb=max(0, int(getattr(plan, "included_gb", 0) or 0)) - int(sub.base_gb or 0),
        )

    invoice = compute_renew_invoice(plan, sub)
    amount = int(invoice["total"])
    if charge_wallet:
        if payer is None:
            raise ValueError("پرداخت‌کننده مشخص نیست")
        from app.services.wallet import debit_wallet

        await debit_wallet(
            session,
            payer,
            amount,
            reason=f"reseller_renew:{sub.pg_username}:g{sub.renew_generation}",
            commit=False,
        )

    days = max(0, int(getattr(plan, "duration_days", 0) or 0))
    now = _utcnow()
    if days > 0:
        anchor = _as_aware(sub.expires_at) or now
        if anchor < now:
            anchor = now
        sub.expires_at = anchor + timedelta(days=days)
    # Keep base from plan (subscription package definition).
    # current_capacity keeps snapped SoT — never reset from included_*=0.
    sub.plan_id = int(plan.id)
    if not is_current_capacity_plan(plan):
        sub.base_gb = max(0, int(getattr(plan, "included_gb", 0) or 0))
        sub.base_users = max(0, int(getattr(plan, "included_users", 0) or 0))
    sub.last_renewed_at = now
    sub.warn_sent_at = None

    try:
        restore_stats = await restore_subscription_access(session, sub, commit=False)
        # Skip PG push when totals are zero (unlimited / unread) so renew
        # cannot clear live limits after a current_capacity grant.
        if (
            int(sub.base_gb or 0)
            or int(sub.base_users or 0)
            or int(sub.extra_gb_purchased or 0)
            or int(sub.extra_users_purchased or 0)
        ):
            await sync_pg_capacity_from_sub(session, sub)
        reset_ok = 0
        reset_err = 0
        if reset_user_traffic:
            from app.services.billing_suspend import list_owned_user_ids
            from app.services.pasarguard import get_pg

            pg = get_pg()
            for uid in await list_owned_user_ids(pg, sub.pg_username):
                try:
                    await pg.reset_user_by_id(uid)
                    reset_ok += 1
                except Exception:
                    reset_err += 1
                    logger.debug("renew reset user %s failed", uid, exc_info=True)
    except Exception as e:
        if charge_wallet and payer is not None and amount > 0:
            from app.services.wallet import credit_wallet

            await credit_wallet(
                session,
                payer,
                amount,
                reason=f"reseller_renew_refund:{sub.pg_username}:g{sub.renew_generation}",
                commit=False,
            )
        await session.commit()
        logger.exception("renew failed admin=%s", sub.pg_username)
        if isinstance(e, ValueError) and str(e).strip():
            raise ValueError(str(e)) from e
        raise ValueError("تمدید ناموفق بود — در صورت کسر، مبلغ بازگردانده شد") from e

    await session.commit()
    return {
        "amount": amount,
        "invoice": invoice,
        "expires_at": sub.expires_at,
        "restore": restore_stats,
        "reset_ok": reset_ok,
        "reset_err": reset_err,
    }


async def apply_addon_plan(
    session: AsyncSession,
    *,
    sub: PgAdminSubscription,
    addon_plan: ResellerPlan,
    payer: BotUser | None,
    charge_wallet: bool = True,
) -> dict[str, Any]:
    """Purchase an addon_volume / addon_users plan onto an active subscription."""
    kind = plan_kind_of(addon_plan)
    if kind not in {PLAN_KIND_ADDON_VOLUME, PLAN_KIND_ADDON_USERS}:
        raise ValueError("این پلن بستهٔ اضافه نیست")
    if not bool(getattr(addon_plan, "is_active", False)):
        raise ValueError("بسته غیرفعال است")
    if sub.access_status != STATUS_ACTIVE:
        raise ValueError("فقط اشتراک فعال می‌تواند بسته اضافه بخرد — ابتدا تمدید کنید")
    if _as_aware(sub.expires_at) is not None and _as_aware(sub.expires_at) <= _utcnow():
        raise ValueError("اشتراک منقضی است — ابتدا تمدید کنید")

    add_gb = max(0, int(getattr(addon_plan, "addon_gb", 0) or 0))
    add_users = max(0, int(getattr(addon_plan, "addon_users", 0) or 0))
    if kind == PLAN_KIND_ADDON_VOLUME:
        if add_gb <= 0:
            raise ValueError("مقدار حجم بسته نامعتبر است")
        add_users = 0
    else:
        if add_users <= 0:
            raise ValueError("تعداد کاربر بسته نامعتبر است")
        add_gb = 0

    # Checked before the charge; the parent stays locked until this purchase commits.
    await assert_child_growth_fits(session, pg_username=sub.pg_username, add_gb=add_gb)
    price = max(0, int(getattr(addon_plan, "price", 0) or 0))
    if charge_wallet:
        if payer is None:
            raise ValueError("پرداخت‌کننده مشخص نیست")
        if price <= 0:
            raise ValueError("قیمت بسته نامعتبر است")
        from app.services.wallet import debit_wallet

        await debit_wallet(
            session,
            payer,
            price,
            reason=f"reseller_addon:{addon_plan.id}:{sub.pg_username}",
            commit=False,
        )

    sub.extra_gb_purchased = int(sub.extra_gb_purchased or 0) + add_gb
    sub.extra_users_purchased = int(sub.extra_users_purchased or 0) + add_users
    try:
        await _assert_owner_capacity_budget(
            session,
            target_pg_username=sub.pg_username,
            next_total_gb=int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0),
            next_total_users=int(sub.base_users or 0) + int(sub.extra_users_purchased or 0),
        )
        await sync_pg_capacity_from_sub(session, sub)
    except Exception as e:
        if charge_wallet and payer is not None and price > 0:
            from app.services.wallet import credit_wallet

            await credit_wallet(
                session,
                payer,
                price,
                reason=f"reseller_addon_refund:{addon_plan.id}:{sub.pg_username}",
                commit=False,
            )
        await session.commit()
        if isinstance(e, ValueError) and str(e).strip():
            raise ValueError(str(e)) from e
        raise ValueError("اعمال بسته در پاسارگارد ناموفق بود — مبلغ بازگردانده شد") from e

    await session.commit()
    return {
        "amount": price,
        "added_gb": add_gb,
        "added_users": add_users,
        "extra_gb": int(sub.extra_gb_purchased or 0),
        "extra_users": int(sub.extra_users_purchased or 0),
        "expires_at": sub.expires_at,
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
    )
    return sub


async def run_subscription_expiry_tick(session: AsyncSession) -> dict[str, int]:
    """Scheduler: expire due subscriptions; skip PAYG-linked plans."""
    now = _utcnow()
    result = await session.execute(
        select(PgAdminSubscription).where(
            PgAdminSubscription.access_status == STATUS_ACTIVE,
            PgAdminSubscription.expires_at.is_not(None),
            PgAdminSubscription.expires_at <= now,
        )
    )
    due = list(result.scalars().all())
    stats = {"expired": 0, "skipped_payg": 0, "errors": 0, "warned": 0}

    # Warnings (active, within WARN_DAYS_BEFORE)
    warn_before = now + timedelta(days=WARN_DAYS_BEFORE)
    warn_q = await session.execute(
        select(PgAdminSubscription).where(
            PgAdminSubscription.access_status == STATUS_ACTIVE,
            PgAdminSubscription.expires_at.is_not(None),
            PgAdminSubscription.expires_at > now,
            PgAdminSubscription.expires_at <= warn_before,
            PgAdminSubscription.warn_sent_at.is_(None),
        )
    )
    for sub in warn_q.scalars().all():
        sub.warn_sent_at = now
        stats["warned"] += 1
        # Notification best-effort (bot may not be available here)
        try:
            await _notify_expiry_warning(session, sub)
        except Exception:
            logger.debug("expiry warn notify failed %s", sub.pg_username, exc_info=True)

    for sub in due:
        # Skip if current plan is PAYG
        plan = None
        if sub.plan_id:
            plan = await session.get(ResellerPlan, int(sub.plan_id))
        if plan is not None and str(getattr(plan, "billing_mode", "") or "").lower() == "payg":
            stats["skipped_payg"] += 1
            continue
        # Also skip if linked reseller profile is PAYG
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
            await expire_subscription(session, sub, commit=False)
            stats["expired"] += 1
            try:
                await _notify_expired(session, sub)
            except Exception:
                logger.debug("expired notify failed %s", sub.pg_username, exc_info=True)
        except Exception:
            stats["errors"] += 1
            logger.exception("expire tick failed admin=%s", sub.pg_username)

    await session.commit()
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


async def list_addon_plans(
    session: AsyncSession,
    *,
    kind: str | None = None,
    category_id: int | None | object = ...,
) -> list[ResellerPlan]:
    """Active reseller capacity packs. Optional strict ``category_id`` filter.

    Omit ``category_id`` for the full catalog (admin lists). Pass an int/None
    to match packs priced for that subscription-plan category.
    """
    q = select(ResellerPlan).where(
        ResellerPlan.is_active.is_(True),
        ResellerPlan.plan_kind.in_([PLAN_KIND_ADDON_VOLUME, PLAN_KIND_ADDON_USERS]),
    )
    if kind in {PLAN_KIND_ADDON_VOLUME, PLAN_KIND_ADDON_USERS}:
        q = select(ResellerPlan).where(
            ResellerPlan.is_active.is_(True),
            ResellerPlan.plan_kind == kind,
        )
    if category_id is not ...:
        if category_id is None:
            q = q.where(ResellerPlan.category_id.is_(None))
        else:
            q = q.where(ResellerPlan.category_id == int(category_id))
    q = q.order_by(ResellerPlan.sort_order, ResellerPlan.id)
    return list((await session.execute(q)).scalars().all())


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
