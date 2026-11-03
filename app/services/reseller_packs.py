"""Capacity packs a shop owner buys for their own panel admin: which packs fit, and the purchase.

A pack without a category fits every subscription category; a categorised pack fits only its own
(the catalog listing is ``list_addon_plans``). A purchase adds to the admin's live panel limit,
not to a stored total, and is claimed against the subscription generation the buyer saw, so a
repeated tap or an old screen buys once.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerPlan, ResellerProfile
from app.services.billing import is_payg
from app.services.pg_admin_subscription import (
    PLAN_KIND_ADDON_VOLUME,
    STATUS_ACTIVE,
    get_or_create_subscription,
    is_addon_plan,
    is_subscription_plan,
    plan_kind_of,
)
from app.services.reseller_capacity import (
    ExtraPurchase,
    apply_paid_extra,
    users_raiser,
    volume_raiser,
)
from app.services.volume_allocation_cascade import assert_child_growth_fits

NO_PACK = "این بسته پیدا نشد یا برای پلن شما در دسترس نیست"
PACK_NOT_FOR_PAYG = "خرید بسته برای پلن PAYG فعال نیست"
PACK_NEEDS_SUBSCRIPTION = "اول یک پلن نمایندگی (اشتراک) بخرید"
PACK_NEEDS_ACTIVE = "فقط اشتراک فعال می‌تواند بسته اضافه بخرد — ابتدا تمدید کنید"
PACK_EXPIRED = "اشتراک منقضی است — ابتدا تمدید کنید"
PACK_BAD_AMOUNT = "مقدار بسته نامعتبر است"
PACK_BAD_PRICE = "قیمت بسته نامعتبر است"
NO_PANEL_ADMIN = "ادمین پاسارگارد برای این نماینده تنظیم نشده است"


def pack_fits_plan(pack: ResellerPlan | None, plan: ResellerPlan | None) -> bool:
    """A general pack fits every plan; a categorised one only a plan of the same category."""
    if pack is None or plan is None or not pack.is_active or not is_addon_plan(pack):
        return False
    if pack.category_id is None:
        return True
    return plan.category_id is not None and int(pack.category_id) == int(plan.category_id)


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored as UTC.
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _pack_amounts(pack: ResellerPlan) -> tuple[int, int]:
    """(gb, users) the pack adds; exactly one of them is positive."""
    if plan_kind_of(pack) == PLAN_KIND_ADDON_VOLUME:
        gb, users = int(pack.addon_gb or 0), 0
    else:
        gb, users = 0, int(pack.addon_users or 0)
    if gb <= 0 and users <= 0:
        raise ValueError(PACK_BAD_AMOUNT)
    return gb, users


async def buy_addon_pack(
    session: AsyncSession,
    *,
    user: BotUser,
    profile: ResellerProfile,
    plan: ResellerPlan | None,
    pack_id: int,
    generation: int,
) -> dict[str, int | str]:
    """Pay a catalog pack from the owner's wallet and raise the admin's live panel limit by it.

    ``profile`` must be the profile of ``user`` (the caller resolves both from the session).
    Raises ``ValueError`` with a message that is safe to show.
    """
    if profile.user_id != user.id:
        raise ValueError(NO_PACK)
    if is_payg(profile):
        raise ValueError(PACK_NOT_FOR_PAYG)
    if plan is None or not is_subscription_plan(plan):
        raise ValueError(PACK_NEEDS_SUBSCRIPTION)
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        raise ValueError(NO_PANEL_ADMIN)
    pack = await session.get(ResellerPlan, int(pack_id))
    if not pack_fits_plan(pack, plan):
        raise ValueError(NO_PACK)
    price = int(pack.price or 0)
    if price <= 0:
        raise ValueError(PACK_BAD_PRICE)
    gb, users = _pack_amounts(pack)
    sub = await get_or_create_subscription(session, uname)
    if sub.access_status != STATUS_ACTIVE:
        raise ValueError(PACK_NEEDS_ACTIVE)
    expires = _aware(sub.expires_at)
    if expires is not None and expires <= datetime.now(timezone.utc):
        raise ValueError(PACK_EXPIRED)
    # Before the charge and the panel call; the parent stays locked until this purchase commits.
    await assert_child_growth_fits(session, pg_username=uname, add_gb=gb)
    purchase = ExtraPurchase(
        uname=uname, amount=price, gb=gb, users=users,
        reason=f"reseller_addon:{int(pack.id)}:{uname}", generation=int(generation),
    )
    apply = volume_raiser(uname, gb) if gb else users_raiser(uname, users)
    new_limit = await apply_paid_extra(session, user=user, purchase=purchase, apply=apply)
    return {"pack": pack.name, "gb": gb, "users": users, "amount": price, "new_limit": new_limit}
