"""A shop owner renewing their own reseller package, optionally moving to another package.

Moving happens at renewal: the owner pays the target package's renewal invoice, the period
extends from the current expiry and the target's capacity applies at once. Self-service is
limited to packages that differ only in capacity, duration and price (same permissions, panel
role, groups and fixed billing), so a package change can never widen or narrow what the shop
may do. A smaller package is offered only when the shop's live usage fits it.

Every renewal carries the subscription's ``renew_generation`` read when the page was shown,
so a repeated tap or form submission renews once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.billing import GB, is_payg
from app.services.pg_admin_subscription import (
    NO_TIME_RENEWAL,
    PLAN_KIND_SUBSCRIPTION,
    compute_renew_invoice,
    get_or_create_subscription,
    get_subscription,
    is_subscription_plan,
    plan_has_time,
    plan_kind_of,
    renew_subscription,
)
from app.services.pg_quota import _as_int
from app.services.reseller_shop_gates import assert_shop_allows_renew
from app.services.resellers import normalize_feature_perms, parse_perms

logger = logging.getLogger(__name__)

RELATION_CURRENT = "current"
RELATION_UPGRADE = "upgrade"
RELATION_DOWNGRADE = "downgrade"
RELATION_OTHER = "other"
RELATION_LABELS = {
    RELATION_CURRENT: "بستهٔ فعلی",
    RELATION_UPGRADE: "ارتقا",
    RELATION_DOWNGRADE: "تنزل",
    RELATION_OTHER: "تغییر بسته",
}
NEEDS_SUPPORT = "تغییر به این بسته با هماهنگی پشتیبانی انجام می‌شود"
STALE_PAGE = "این صفحه قدیمی است یا تمدید دیگری همین حالا انجام شد؛ صفحه را دوباره باز کنید"
USAGE_UNREADABLE = "مصرف فعلی از پنل خوانده نشد؛ کمی بعد دوباره تلاش کنید"
PACKAGE_LIST_LIMIT = 30
RENEW_EXPLAIN = (
    "با تمدید، مدت بسته از تاریخ انقضای فعلی اضافه می‌شود، دسترسی باز می‌شود و مصرف خود ادمین (حجم بسته) "
    "صفر می‌شود. مصرف مشتریان شما دست‌نخورده می‌ماند. هزینه از کیف پول کسر می‌شود."
)
MOVE_UNTIMED_EXPLAIN = (
    "ظرفیت بستهٔ جدید همین حالا اعمال می‌شود. این بسته زمان ندارد، پس زمان و مصرف تغییری نمی‌کند. "
    "هزینه از کیف پول کسر می‌شود."
)


@dataclass(slots=True)
class AdminUsage:
    users: int
    used_bytes: int


@dataclass(slots=True)
class PackageChoice:
    plan: ResellerPlan
    relation: str
    invoice: dict
    blocked: str | None

    @property
    def label(self) -> str:
        return RELATION_LABELS[self.relation]


@dataclass(slots=True)
class PackageOverview:
    sub: PgAdminSubscription | None
    current: ResellerPlan | None
    generation: int
    choices: list[PackageChoice]
    usage: AdminUsage | None

    @property
    def expires_at_iso(self) -> str | None:
        expires = self.sub.expires_at if self.sub is not None else None
        if expires is None:
            return None
        # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
        return (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)).isoformat()


def _limits(plan: ResellerPlan, sub: PgAdminSubscription | None) -> tuple[int, int]:
    """(users, gb) the shop would have on ``plan`` with its bought extras; 0 = unlimited."""
    users = max(0, int(plan.included_users or 0))
    gb = max(0, int(plan.included_gb or 0))
    if sub is not None:
        users = users + int(sub.extra_users_purchased or 0) if users else 0
        gb = gb + int(sub.extra_gb_purchased or 0) if gb else 0
    return users, gb


def _rank(value: int) -> float:
    return float("inf") if value <= 0 else float(value)


def package_relation(current: ResellerPlan | None, target: ResellerPlan) -> str:
    if current is None or int(target.id) == int(current.id):
        return RELATION_CURRENT
    cur = (_rank(int(current.included_users or 0)), _rank(int(current.included_gb or 0)))
    new = (_rank(int(target.included_users or 0)), _rank(int(target.included_gb or 0)))
    if new == cur:
        return RELATION_UPGRADE if int(target.price or 0) > int(current.price or 0) else RELATION_OTHER
    if all(n >= c for n, c in zip(new, cur, strict=True)):
        return RELATION_UPGRADE
    if all(n <= c for n, c in zip(new, cur, strict=True)):
        return RELATION_DOWNGRADE
    return RELATION_OTHER


def _group_set(plan: ResellerPlan) -> frozenset[str]:
    return frozenset(p.strip() for p in str(plan.pg_group_ids or "").split(",") if p.strip())


def _perm_set(plan: ResellerPlan) -> frozenset[str]:
    return frozenset(parse_perms(normalize_feature_perms(plan.web_permissions or plan.bot_permissions)))


def _is_fixed_subscription(plan: ResellerPlan) -> bool:
    billing = str(plan.billing_mode or "fixed").strip().lower()
    return plan_kind_of(plan) == PLAN_KIND_SUBSCRIPTION and billing == "fixed"


def switch_blocker(current: ResellerPlan | None, target: ResellerPlan) -> str | None:
    """Why the owner may not move to ``target`` alone, or None when they may."""
    if not target.is_active or bool(target.is_system) or not _is_fixed_subscription(target):
        return "این بسته برای تمدید در دسترس نیست"
    if current is None or not _is_fixed_subscription(current):
        return NEEDS_SUPPORT
    same_rules = (
        target.pg_role_id == current.pg_role_id
        and _group_set(target) == _group_set(current)
        and _perm_set(target) == _perm_set(current)
        and bool(target.create_pg_admin) == bool(current.create_pg_admin)
    )
    return None if same_rules else NEEDS_SUPPORT


def usage_blocker(target: ResellerPlan, sub: PgAdminSubscription | None, usage: AdminUsage) -> str | None:
    users, gb = _limits(target, sub)
    if users and usage.users > users:
        return f"الان {usage.users} کاربر دارید و این بسته {users} کاربر ظرفیت دارد"
    if gb and usage.used_bytes > gb * GB:
        used_gb = -(-usage.used_bytes // GB)
        return f"الان {used_gb} گیگ مصرف شده و این بسته {gb} گیگ حجم دارد"
    return None


async def read_admin_usage(pg_username: str) -> AdminUsage | None:
    """Live users and used traffic of the shop's panel admin; None when unreadable."""
    # Deferred: the panel client pulls in the HTTP stack, not needed to list packages offline.
    from app.services.pasarguard import get_pg

    try:
        admin = await get_pg().get_admin(pg_username)
    except Exception:
        logger.warning("read admin usage failed admin=%s", pg_username, exc_info=True)
        return None
    if not isinstance(admin, dict) or not admin:
        return None
    users = _as_int(admin.get("total_users")) or _as_int(admin.get("users_count")) or 0
    used = _as_int(admin.get("used_traffic")) or _as_int(admin.get("traffic_used")) or 0
    return AdminUsage(users=max(0, users), used_bytes=max(0, used))


def _pg_username(profile: ResellerProfile) -> str:
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        raise ValueError("ادمین پاسارگارد برای این نماینده تنظیم نشده است")
    return uname


async def _subscription_plans(session: AsyncSession) -> list[ResellerPlan]:
    return list(
        await session.scalars(
            select(ResellerPlan)
            .where(ResellerPlan.is_active.is_(True), ResellerPlan.plan_kind == PLAN_KIND_SUBSCRIPTION)
            .order_by(ResellerPlan.sort_order, ResellerPlan.id)
            .limit(PACKAGE_LIST_LIMIT)
        )
    )


async def package_overview(session: AsyncSession, profile: ResellerProfile) -> PackageOverview:
    """The current package first, then every package the owner could renew into."""
    if is_payg(profile):
        raise ValueError("پلن PAYG تمدید زمانی ندارد — از شارژ کیف پول استفاده کنید")
    uname = _pg_username(profile)
    sub = await get_subscription(session, uname)
    current = await session.get(ResellerPlan, int(profile.plan_id)) if profile.plan_id else None
    usage = await read_admin_usage(uname)
    choices: list[PackageChoice] = []
    plans = await _subscription_plans(session)
    if current is not None and is_subscription_plan(current) and all(int(p.id) != int(current.id) for p in plans):
        plans.insert(0, current)
    for plan in plans:
        relation = package_relation(current, plan)
        blocked = NO_TIME_RENEWAL if relation == RELATION_CURRENT and not plan_has_time(plan) else (
            None if relation == RELATION_CURRENT else switch_blocker(current, plan)
        )
        if blocked is None and relation != RELATION_CURRENT and relation != RELATION_UPGRADE:
            blocked = usage_blocker(plan, sub, usage) if usage is not None else None
        choices.append(PackageChoice(plan=plan, relation=relation, invoice=compute_renew_invoice(plan, sub), blocked=blocked))
    choices.sort(key=lambda c: (c.relation != RELATION_CURRENT, c.blocked is not None))
    return PackageOverview(
        sub=sub, current=current, generation=int(sub.renew_generation or 0) if sub else 0, choices=choices,
        usage=usage,
    )


async def _assert_target_allowed(
    *, current: ResellerPlan | None, target: ResellerPlan, sub: PgAdminSubscription, uname: str,
) -> None:
    relation = package_relation(current, target)
    if relation == RELATION_CURRENT:
        if not is_subscription_plan(target):
            raise ValueError("تمدید فقط برای پلن اشتراک است")
        return
    blocked = switch_blocker(current, target)
    if blocked:
        raise ValueError(blocked)
    if relation == RELATION_UPGRADE:
        return
    usage = await read_admin_usage(uname)
    if usage is None:
        raise ValueError(USAGE_UNREADABLE)
    blocked = usage_blocker(target, sub, usage)
    if blocked:
        raise ValueError(blocked)


def renewal_effect(result: dict) -> str:
    """One sentence on what a finished renewal did to the volume, for every screen that reports it."""
    if not result.get("timed"):
        return "ظرفیت بستهٔ جدید اعمال شد؛ زمان و مصرف تغییری نکرد."
    if result.get("usage_reset"):
        return "مصرف ادمین صفر شد؛ مصرف کاربران شما دست‌نخورده ماند."
    return "مصرف ادمین صفر نشد؛ برای بررسی با پشتیبانی تماس بگیرید."


async def renew_package(
    session: AsyncSession,
    *,
    owner: BotUser,
    profile: ResellerProfile,
    plan_id: int,
    generation: int,
) -> dict:
    """Renew into ``plan_id`` (the current package or an allowed one), once per shown page.

    Renewing a timed package starts its next period (time extended, the admin's volume refilled,
    customers' usage untouched). A package without time is not renewed: moving to it applies its
    capacity only.
    """
    # Expiry flips is_active off; renew must still work. Soft-deactivate while open is refused.
    await assert_shop_allows_renew(session, profile)
    if is_payg(profile):
        raise ValueError("پلن PAYG تمدید زمانی ندارد — از شارژ کیف پول استفاده کنید")
    uname = _pg_username(profile)
    target = await session.get(ResellerPlan, int(plan_id))
    if target is None:
        raise ValueError("بسته پیدا نشد")
    current = await session.get(ResellerPlan, int(profile.plan_id)) if profile.plan_id else None
    sub = await get_or_create_subscription(session, uname)
    # renew_subscription claims this same generation atomically; a stale page fails here first.
    if int(sub.renew_generation or 0) != int(generation):
        raise ValueError(STALE_PAGE)
    await _assert_target_allowed(current=current, target=target, sub=sub, uname=uname)
    changed = current is None or int(target.id) != int(current.id)
    profile_id = int(profile.id)
    result = await renew_subscription(
        session, sub=sub, plan=target, payer=owner, charge_wallet=True, moving=changed,
    )
    if changed:
        fresh = await session.get(ResellerProfile, profile_id)
        fresh.plan_id = int(target.id)
        await session.commit()
    out = {**result, "plan_name": target.name, "changed": changed}
    try:
        from app.services.reseller_purchase_notify import notify_package_wallet_renew

        await notify_package_wallet_renew(
            session,
            buyer=owner,
            amount=int(out.get("amount") or 0),
            plan_name=target.name,
            changed=changed,
        )
    except Exception:
        logger.warning(
            "package renew owner notify failed user=%s plan=%s",
            getattr(owner, "id", None),
            getattr(target, "id", None),
            exc_info=True,
        )
    return out
