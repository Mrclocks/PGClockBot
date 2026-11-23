"""What a shop's automatic discounts did: uses and money per rule, issued and used coupons."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LoyaltyDiscountEntitlement, Order, OrderDiscount, OrderStatus
from app.services.coupon_programs import TRIGGERS, shop_key as coupon_shop_key
from app.services.order_discounts import SOURCE_RULE

REPORT_DAYS = 30


@dataclass(frozen=True, slots=True)
class RuleResult:
    orders: int
    discount: int
    sales: int


@dataclass(frozen=True, slots=True)
class CouponResult:
    issued: int
    used: int


async def rule_results(session: AsyncSession, shop_id: int | None, *, since: datetime) -> dict[int, RuleResult]:
    """Delivered orders each rule lowered since ``since``: count, discount given and amount paid."""
    rows = await session.execute(
        select(OrderDiscount.source_ref, func.count(), func.sum(OrderDiscount.amount), func.sum(Order.amount))
        .join(Order, Order.id == OrderDiscount.order_id)
        .where(
            OrderDiscount.shop_key == int(shop_id or 0), OrderDiscount.source == SOURCE_RULE,
            OrderDiscount.released_at.is_(None), OrderDiscount.created_at >= since,
            Order.status == OrderStatus.DELIVERED.value,
        )
        .group_by(OrderDiscount.source_ref)
    )
    return {
        int(ref): RuleResult(orders=int(count), discount=int(discount or 0), sales=int(sales or 0))
        for ref, count, discount, sales in rows.all() if (ref or "").isdigit()
    }


async def coupon_results(session: AsyncSession, shop_id: int | None, *, since: datetime) -> dict[str, CouponResult]:
    """Coupons each program issued since ``since`` and how many of them were used."""
    results: dict[str, CouponResult] = {}
    for trigger in TRIGGERS:
        issued, used = (await session.execute(
            select(func.count(), func.coalesce(func.sum(case((LoyaltyDiscountEntitlement.status == "consumed", 1), else_=0)), 0))
            .where(
                LoyaltyDiscountEntitlement.issue_key.startswith(f"{trigger}:{coupon_shop_key(shop_id)}:", autoescape=True),
                LoyaltyDiscountEntitlement.created_at >= since,
            )
        )).one()
        results[trigger] = CouponResult(issued=int(issued or 0), used=int(used or 0))
    return results
