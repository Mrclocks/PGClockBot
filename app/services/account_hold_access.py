"""Who may turn which customer or shop off, resolved from the signed-in staff, never from a form.

- The platform owner: any customer (account-wide) and any shop.
- Shop staff holding ``accounts_disable``: their own customers (in that shop only) and, for a
  level-1 shop, its own level-2 shops.

A hold is released by the platform owner or by staff of the shop that placed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountHold, BotUser, OrgPrincipal, ResellerProfile
from app.services.account_holds import KIND_CUSTOMER, KIND_SHOP, PLATFORM_SCOPE, open_hold_for
from app.services.authz import authz_from_staff, can_shop
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_l2_lifecycle import (
    PrincipalLifecycleError,
    assert_level2_managed_by,
    load_management_actor,
)
from app.services.shop_manual_adjust import adjustable_customer
from app.services.shop_scope import shop_owner_id
from app.services.users import is_protected_admin

ACCOUNTS_DISABLE_PERMISSION = "accounts_disable"
DENIED = "دسترسی غیرفعال‌سازی حساب ندارید"
NO_CUSTOMER = "کاربر یافت نشد"
PROTECTED = "حساب ادمین را نمی‌توان غیرفعال کرد"
NO_SHOP = "نماینده یافت نشد"
NOT_YOUR_SUBSHOP = "این فروشگاه زیرمجموعهٔ شما نیست"
NOT_YOUR_HOLD = "این غیرفعال‌سازی را مالک یا فروشگاه دیگری انجام داده است"


@dataclass(frozen=True, slots=True)
class CustomerTarget:
    customer: BotUser
    scope_shop_id: int
    placed_by_shop_id: int


@dataclass(frozen=True, slots=True)
class ShopTarget:
    profile: ResellerProfile
    placed_by_shop_id: int


def _actor_shop(staff: dict[str, Any] | None) -> int:
    """The staff's own shop when it holds the permission; raises ``PermissionError``."""
    if not can_shop(authz_from_staff(staff), ACCOUNTS_DISABLE_PERMISSION):
        raise PermissionError(DENIED)
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError(DENIED)
    return int(shop_id)


async def customer_target(session: AsyncSession, staff: dict[str, Any] | None, customer_id: int) -> CustomerTarget:
    if is_explicit_owner_staff(staff):
        customer = await session.get(BotUser, int(customer_id))
        if customer is None:
            raise ValueError(NO_CUSTOMER)
        if is_protected_admin(customer):
            raise ValueError(PROTECTED)
        return CustomerTarget(customer, PLATFORM_SCOPE, PLATFORM_SCOPE)
    shop_id = _actor_shop(staff)
    customer = await adjustable_customer(session, shop_id, int(customer_id))
    return CustomerTarget(customer, shop_id, shop_id)


async def _profile(session: AsyncSession, shop_user_id: int) -> ResellerProfile:
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_user_id)))
    if profile is None:
        raise ValueError(NO_SHOP)
    return profile


async def shop_target(session: AsyncSession, staff: dict[str, Any] | None, shop_user_id: int) -> ShopTarget:
    profile = await _profile(session, shop_user_id)
    if is_explicit_owner_staff(staff):
        return ShopTarget(profile, PLATFORM_SCOPE)
    actor_shop = _actor_shop(staff)
    if int(profile.user_id) == actor_shop:
        raise PermissionError(NOT_YOUR_SUBSHOP)
    principal_id = await session.scalar(
        select(OrgPrincipal.id).where(OrgPrincipal.reseller_profile_id == int(profile.id)).limit(1)
    )
    try:
        actor = await load_management_actor(session, staff)
        if principal_id is None:
            raise PrincipalLifecycleError(NOT_YOUR_SUBSHOP, code="out_of_scope")
        await assert_level2_managed_by(session, actor, int(principal_id))
    except PrincipalLifecycleError as exc:
        raise PermissionError(NOT_YOUR_SUBSHOP) from exc
    return ShopTarget(profile, actor_shop)


def assert_may_release(hold: AccountHold, staff: dict[str, Any] | None, placed_by_shop_id: int) -> None:
    """Only the owner, or the shop that placed the hold, may lift it."""
    if is_explicit_owner_staff(staff):
        return
    if int(hold.placed_by_shop_id) != int(placed_by_shop_id):
        raise PermissionError(NOT_YOUR_HOLD)


async def open_customer_hold_of(session: AsyncSession, target: CustomerTarget) -> AccountHold | None:
    return await open_hold_for(
        session, kind=KIND_CUSTOMER, target_user_id=int(target.customer.id), scope_shop_id=target.scope_shop_id
    )


async def open_shop_hold_of(session: AsyncSession, target: ShopTarget) -> AccountHold | None:
    uid = int(target.profile.user_id)
    return await open_hold_for(session, kind=KIND_SHOP, target_user_id=uid, scope_shop_id=uid)
