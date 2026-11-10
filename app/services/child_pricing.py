"""Level-1 shops price and back their level-2 shops.

A level-1 shop keeps its own PAYG price lists (``ChildPricePlan``), assigns one to each
of its level-2 shops and may hand them credit out of its own line. Charges then run in
two steps (``payg_charges.reserve_charge``): the level-2 shop pays its parent's price,
the parent pays the owner's price, in one transaction.

Every call is scoped by the level-1 shop resolved from the signed-in staff
(``parent_scope``); a child that is not a direct, active level-2 shop of that parent, or
a plan of another parent, is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChildPricePlan, OrgPrincipal, ResellerProfile
from app.services.billing import BILLING_MODE_FIXED, BILLING_MODE_PAYG
from app.services.org_principals import DEPTH_ONE, DEPTH_TWO, STATUS_ACTIVE
from app.services.payg_pricing import PaygPriceError, PaygRates, assert_billable_rates
from app.services.platform_identity import is_explicit_owner_staff
from app.services.representative_unification import staff_can_manage_representatives
from app.services.shop_credit import (
    CreditLimitChange,
    child_shop_ids,
    credit_account,
    effective_limit,
    reserved_for_children,
    set_credit_limit,
)
from app.services.shop_scope import shop_owner_id

PLAN_LIMIT = 50


class ChildPricingError(ValueError):
    """Refused; the message is safe to show to the level-1 shop."""


def parent_scope(staff: Mapping[str, Any]) -> int:
    """The level-1 shop the signed-in staff manages children for; raises ``PermissionError``."""
    if is_explicit_owner_staff(dict(staff)) or not staff_can_manage_representatives(staff):
        raise PermissionError("مدیریت قیمت زیرمجموعه فقط برای نمایندهٔ سطح ۱ است")
    shop_id = shop_owner_id(dict(staff))
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return int(shop_id)


async def parent_shop_profile(session: AsyncSession, profile: ResellerProfile) -> ResellerProfile | None:
    """The level-1 shop above this level-2 shop, or ``None`` for any other shop."""
    child = await session.scalar(select(OrgPrincipal).where(
        OrgPrincipal.reseller_profile_id == int(profile.id), OrgPrincipal.depth == DEPTH_TWO,
        OrgPrincipal.status == STATUS_ACTIVE,
    ).limit(1))
    if child is None or not child.parent_id:
        return None
    parent = await session.get(OrgPrincipal, int(child.parent_id))
    if parent is None or int(parent.depth) != DEPTH_ONE or str(parent.status) != STATUS_ACTIVE:
        return None
    if not parent.reseller_profile_id:
        return None
    return await session.get(ResellerProfile, int(parent.reseller_profile_id))


@dataclass(frozen=True, slots=True)
class ChildPlanInput:
    name: str
    basis: str
    price_per_gb: int
    user_price: int
    user_price_per_30d: bool
    unlimited_price: int | None


def _validated(data: ChildPlanInput) -> ChildPlanInput:
    if not data.name.strip():
        raise ChildPricingError("نام پلن الزامی است")
    try:
        assert_billable_rates(PaygRates(
            basis=data.basis, charge_upfront=True, price_per_gb=data.price_per_gb, user_price=data.user_price,
            user_price_per_30d=data.user_price_per_30d, unlimited_price=data.unlimited_price,
        ))
    except PaygPriceError as exc:
        raise ChildPricingError(str(exc)) from exc
    return data


async def list_child_plans(session: AsyncSession, parent_shop_id: int) -> list[ChildPricePlan]:
    return list((await session.scalars(
        select(ChildPricePlan).where(ChildPricePlan.owner_shop_id == int(parent_shop_id))
        .order_by(ChildPricePlan.id).limit(PLAN_LIMIT)
    )).all())


async def create_child_plan(session: AsyncSession, parent_shop_id: int, data: ChildPlanInput) -> ChildPricePlan:
    data = _validated(data)
    if len(await list_child_plans(session, parent_shop_id)) >= PLAN_LIMIT:
        raise ChildPricingError(f"حداکثر {PLAN_LIMIT} پلن مجاز است")
    plan = ChildPricePlan(
        owner_shop_id=int(parent_shop_id), name=data.name.strip()[:128], payg_basis=data.basis,
        payg_charge_upfront=True, price_per_gb=data.price_per_gb, payg_user_price=data.user_price,
        payg_user_price_per_30d=data.user_price_per_30d, payg_unlimited_price=data.unlimited_price,
    )
    session.add(plan)
    await session.commit()
    return plan


async def _own_plan(session: AsyncSession, parent_shop_id: int, plan_id: int) -> ChildPricePlan:
    plan = await session.scalar(select(ChildPricePlan).where(
        ChildPricePlan.id == int(plan_id), ChildPricePlan.owner_shop_id == int(parent_shop_id)))
    if plan is None:
        raise ChildPricingError("پلن قیمت یافت نشد")
    return plan


async def update_child_plan(
    session: AsyncSession, parent_shop_id: int, plan_id: int, data: ChildPlanInput
) -> ChildPricePlan:
    """Change a price list; charges already taken keep the amount they were taken at."""
    data = _validated(data)
    plan = await _own_plan(session, parent_shop_id, plan_id)
    plan.name = data.name.strip()[:128]
    plan.payg_basis = data.basis
    plan.price_per_gb = data.price_per_gb
    plan.payg_user_price = data.user_price
    plan.payg_user_price_per_30d = data.user_price_per_30d
    plan.payg_unlimited_price = data.unlimited_price
    await session.commit()
    return plan


async def delete_child_plan(session: AsyncSession, parent_shop_id: int, plan_id: int) -> None:
    """Delete a price list no level-2 shop is priced with; one in use is refused."""
    await _own_plan(session, parent_shop_id, plan_id)
    in_use = exists().where(ResellerProfile.child_plan_id == ChildPricePlan.id)
    result = await session.execute(
        delete(ChildPricePlan)
        .where(ChildPricePlan.id == int(plan_id), ChildPricePlan.owner_shop_id == int(parent_shop_id), ~in_use)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ChildPricingError("این پلن به زیرمجموعه‌ای وصل است؛ اول پلن آن زیرمجموعه را عوض کنید")
    await session.commit()


async def _own_child(session: AsyncSession, parent_shop_id: int, child_shop_id: int) -> ResellerProfile:
    if int(child_shop_id) not in await child_shop_ids(session, parent_shop_id):
        raise ChildPricingError("این فروشگاه زیرمجموعهٔ شما نیست")
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(child_shop_id)))
    if profile is None:
        raise ChildPricingError("این فروشگاه زیرمجموعهٔ شما نیست")
    return profile


async def assign_child_plan(
    session: AsyncSession, parent_shop_id: int, child_shop_id: int, plan_id: int | None
) -> None:
    """Price a level-2 shop with one of its parent's plans (``None`` returns it to the owner's package)."""
    profile = await _own_child(session, parent_shop_id, child_shop_id)
    if plan_id is None:
        profile.child_plan_id = None
        profile.billing_mode = BILLING_MODE_FIXED
    else:
        plan = await _own_plan(session, parent_shop_id, int(plan_id))
        profile.child_plan_id = int(plan.id)
        profile.billing_mode = BILLING_MODE_PAYG
        profile.payg_wallet_linked = True
    await session.commit()


async def set_child_credit(
    session: AsyncSession, parent_shop_id: int, child_shop_id: int, limit: int, *, actor: str
) -> None:
    """Hand a level-2 shop credit out of the parent's own free line."""
    await _own_child(session, parent_shop_id, child_shop_id)
    now = datetime.now(timezone.utc)
    own = effective_limit(await credit_account(session, parent_shop_id), now=now)
    free = own - await reserved_for_children(session, parent_shop_id, now=now, except_shop_id=int(child_shop_id))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ChildPricingError("سقف اعتبار نامعتبر است")
    if limit > max(0, free):
        raise ChildPricingError("این سقف از اعتبار آزاد خود شما بیشتر است")
    await set_credit_limit(session, int(child_shop_id), CreditLimitChange(limit=limit), actor=actor)
