"""What a shop owner sees of their own reseller subscription: capacity, time, wallet and buyable packs.

One read model for the Mini App «اشتراک نمایندگی» screen. It reads local rows plus, best effort,
the panel admin's live usage; a panel that does not answer leaves the usage out instead of failing
the screen. The ``generation`` it returns must come back with the next purchase or renewal.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerPlan, ResellerProfile
from app.services.billing import GB, is_payg
from app.services.pg_admin_subscription import (
    NO_TIME_RENEWAL,
    PLAN_KIND_ADDON_VOLUME,
    STATUS_ACTIVE,
    compute_renew_invoice,
    get_subscription,
    is_subscription_plan,
    list_addon_plans,
    plan_has_time,
    plan_kind_of,
)
from app.services.reseller_capacity import (
    ALLOWED_EXTRA_GB,
    ALLOWED_EXTRA_USERS,
    plan_allows_buy_extra,
    plan_extra_gb_price,
    plan_extra_user_price,
)
from app.services.reseller_package import read_admin_usage
from app.services.wallet import get_wallet_balance

logger = logging.getLogger(__name__)

USAGE_READ_TIMEOUT_SEC = 6.0
SECONDS_PER_DAY = 86400
NOT_AVAILABLE_PAYG = "پلن PAYG بسته و تمدید زمانی ندارد؛ از شارژ کیف پول استفاده کنید"
NOT_AVAILABLE_EXPIRED = "اشتراک منقضی شده است؛ اول تمدید کنید"
NOT_AVAILABLE_NO_PLAN = "هنوز پلن نمایندگی (اشتراک) ندارید"


@dataclass(slots=True)
class PackRow:
    id: int
    name: str
    description: str
    gb: int
    users: int
    price: int


@dataclass(slots=True)
class UnitOffer:
    unit_price: int
    options: list[int]


@dataclass(slots=True)
class SubscriptionOverview:
    wallet: int
    plan_name: str
    is_payg: bool
    status: str
    expires_at: str | None
    days_left: int | None
    base_gb: int
    extra_gb: int
    base_users: int
    extra_users: int
    generation: int
    usage: dict[str, int] | None
    renew_total: int | None
    renew_days: int
    renew_note: str | None
    unavailable: str | None
    packs: list[PackRow] = field(default_factory=list)
    unit_gb: UnitOffer | None = None
    unit_users: UnitOffer | None = None

    def payload(self) -> dict:
        return asdict(self)


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _days_left(expires: datetime | None, now: datetime) -> int | None:
    if expires is None:
        return None
    seconds = (expires - now).total_seconds()
    return max(0, -(-int(seconds) // SECONDS_PER_DAY))


def _pack_row(pack: ResellerPlan) -> PackRow:
    volume = plan_kind_of(pack) == PLAN_KIND_ADDON_VOLUME
    return PackRow(
        id=int(pack.id), name=str(pack.name or ""), description=str(pack.description or ""),
        gb=int(pack.addon_gb or 0) if volume else 0, users=0 if volume else int(pack.addon_users or 0),
        price=int(pack.price or 0),
    )


async def _live_usage(uname: str) -> dict[str, int] | None:
    if not uname:
        return None
    try:
        usage = await asyncio.wait_for(read_admin_usage(uname), USAGE_READ_TIMEOUT_SEC)
    except TimeoutError:
        logger.warning("reseller usage read timed out admin=%s", uname)
        return None
    if usage is None:
        return None
    return {"users": usage.users, "used_gb": usage.used_bytes // GB}


def _unavailable(plan: ResellerPlan | None, profile: ResellerProfile, status: str, expired: bool) -> str | None:
    if is_payg(profile):
        return NOT_AVAILABLE_PAYG
    if plan is None or not is_subscription_plan(plan):
        return NOT_AVAILABLE_NO_PLAN
    if status != STATUS_ACTIVE or expired:
        return NOT_AVAILABLE_EXPIRED
    return None


async def build_overview(session: AsyncSession, *, user: BotUser, profile: ResellerProfile) -> SubscriptionOverview:
    """The owner's subscription screen. ``profile`` must already be checked to belong to ``user``."""
    uname = (profile.pg_admin_username or "").strip()
    plan = await session.get(ResellerPlan, int(profile.plan_id)) if profile.plan_id else None
    sub = await get_subscription(session, uname)
    now = datetime.now(timezone.utc)
    expires = _aware(sub.expires_at) if sub is not None else None
    status = str(sub.access_status) if sub is not None else STATUS_ACTIVE
    unavailable = _unavailable(plan, profile, status, expires is not None and expires <= now)
    invoice = compute_renew_invoice(plan, sub) if plan is not None and unavailable != NOT_AVAILABLE_PAYG else None
    overview = SubscriptionOverview(
        wallet=await get_wallet_balance(session, user, shop_id=None),
        plan_name=str(plan.name or "") if plan is not None else "",
        is_payg=is_payg(profile),
        status=status,
        expires_at=expires.isoformat() if expires is not None else None,
        days_left=_days_left(expires, now),
        base_gb=int(sub.base_gb or 0) if sub else 0,
        extra_gb=int(sub.extra_gb_purchased or 0) if sub else 0,
        base_users=int(sub.base_users or 0) if sub else 0,
        extra_users=int(sub.extra_users_purchased or 0) if sub else 0,
        generation=int(sub.renew_generation or 0) if sub else 0,
        usage=await _live_usage(uname),
        renew_total=int(invoice["total"]) if invoice and plan_has_time(plan) else None,
        renew_days=int(invoice["duration_days"]) if invoice else 0,
        renew_note=NO_TIME_RENEWAL if invoice and not plan_has_time(plan) else None,
        unavailable=unavailable,
    )
    if unavailable is None and plan is not None:
        overview.packs = [_pack_row(p) for p in await list_addon_plans(session, category_id=plan.category_id)]
        if plan_allows_buy_extra(plan):
            if plan_extra_gb_price(plan) > 0:
                overview.unit_gb = UnitOffer(plan_extra_gb_price(plan), sorted(ALLOWED_EXTRA_GB))
            if plan_extra_user_price(plan) > 0:
                overview.unit_users = UnitOffer(plan_extra_user_price(plan), sorted(ALLOWED_EXTRA_USERS))
    return overview
