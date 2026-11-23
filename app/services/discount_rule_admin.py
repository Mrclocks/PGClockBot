"""Managing a shop's automatic discount rules: who may, validation, and storage.

Every read and write is scoped by the shop resolved from the signed-in staff
(``discount_rule_scope``), never by a form field, so one shop cannot see or change another's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscountRule
from app.services.authz import authz_from_staff, can_shop
from app.services.discount_rules import AUDIENCE_EARLY_RENEWAL, AUDIENCE_REPEAT, AUDIENCE_RETURNING, AUDIENCES, BPS, EVENTS, MAX_REDUCTION_BPS, RULE_LIMIT
from app.services.gift_codes import check_code_plans
from app.services.shop_scope import is_platform_admin, shop_owner_id

DISCOUNT_RULES_PERMISSION = "discount_rules"
MAX_MONEY = 2_000_000_000
MAX_DAYS = 3650
MAX_COUNT = 100_000
MAX_PRIORITY = 1_000
DAY_AUDIENCES = (AUDIENCE_RETURNING, AUDIENCE_EARLY_RENEWAL)


def discount_rule_scope(staff: Mapping[str, Any]) -> int | None:
    """Shop whose rules ``staff`` manages (``None`` = platform); raises ``PermissionError``."""
    if is_platform_admin(dict(staff)):
        return None
    if not can_shop(authz_from_staff(staff), DISCOUNT_RULES_PERMISSION):
        raise PermissionError("دسترسی مدیریت تخفیف خودکار ندارید")
    shop_id = shop_owner_id(dict(staff))
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


@dataclass(frozen=True, slots=True)
class RuleInput:
    name: str
    audience: str
    min_purchases: int
    audience_days: int
    events: str
    plan_ids: str | None
    mode: str
    percent_bps: int
    fixed_amount: int
    max_per_order: int | None
    min_order_amount: int
    customer_max_count: int | None
    max_uses: int | None
    ends_at: datetime | None
    priority: int


def _bounded(value: object, upper: int, label: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not int or not 0 <= value <= upper:
        raise ValueError(f"«{label}» خارج از محدودهٔ مجاز است")


def _validate_audience(data: RuleInput) -> None:
    if data.audience not in AUDIENCES:
        raise ValueError("گروه مشتریان نامعتبر است")
    _bounded(data.min_purchases, MAX_COUNT, "تعداد خرید قبلی")
    _bounded(data.audience_days, MAX_DAYS, "تعداد روز")
    if data.audience == AUDIENCE_REPEAT and data.min_purchases < 1:
        raise ValueError("برای مشتریان قبلی، حداقل یک خرید قبلی لازم است")
    if data.audience in DAY_AUDIENCES and data.audience_days < 1:
        raise ValueError("تعداد روز باید حداقل ۱ باشد")


def _validate_reduction(data: RuleInput) -> None:
    if data.mode not in {"percent", "fixed"}:
        raise ValueError("نوع تخفیف نامعتبر است")
    _bounded(data.percent_bps, MAX_REDUCTION_BPS, "درصد")
    _bounded(data.fixed_amount, MAX_MONEY, "مبلغ ثابت")
    _bounded(data.max_per_order, MAX_MONEY, "سقف هر سفارش", optional=True)
    _bounded(data.min_order_amount, MAX_MONEY, "حداقل خرید")
    if (data.percent_bps if data.mode == "percent" else data.fixed_amount) <= 0:
        raise ValueError("درصد یا مبلغ تخفیف باید بیشتر از صفر باشد")


def validate_rule(data: RuleInput) -> None:
    """Raise ``ValueError`` (safe to show) unless every field is in range."""
    if not isinstance(data.name, str) or not 1 <= len(data.name.strip()) <= 128:
        raise ValueError("نام قانون باید بین ۱ تا ۱۲۸ نویسه باشد")
    _validate_audience(data)
    _validate_reduction(data)
    events = data.events.split(",") if isinstance(data.events, str) else []
    if not events or any(event not in EVENTS for event in events):
        raise ValueError("حداقل یک نوع خرید را انتخاب کنید")
    _bounded(data.customer_max_count, MAX_COUNT, "سقف هر مشتری", optional=True)
    _bounded(data.max_uses, MAX_COUNT, "سقف کل استفاده", optional=True)
    _bounded(data.priority, MAX_PRIORITY, "اولویت")


def _shop_scope(shop_id: int | None):
    return DiscountRule.shop_id.is_(None) if shop_id is None else DiscountRule.shop_id == int(shop_id)


async def list_rules(session: AsyncSession, shop_id: int | None) -> list[DiscountRule]:
    return list((await session.scalars(
        select(DiscountRule).where(_shop_scope(shop_id))
        .order_by(DiscountRule.priority.desc(), DiscountRule.id).limit(RULE_LIMIT)
    )).all())


async def _own_rule(session: AsyncSession, shop_id: int | None, rule_id: int) -> DiscountRule:
    rule = await session.scalar(select(DiscountRule).where(DiscountRule.id == int(rule_id), _shop_scope(shop_id)))
    if rule is None:
        raise ValueError("قانون تخفیف یافت نشد")
    return rule


async def save_rule(session: AsyncSession, shop_id: int | None, data: RuleInput, *, rule_id: int | None = None) -> DiscountRule:
    validate_rule(data)
    await check_code_plans(session, data.plan_ids, shop_id=shop_id)
    if rule_id is None:
        count = await session.scalar(select(func.count()).select_from(DiscountRule).where(_shop_scope(shop_id)))
        if int(count or 0) >= RULE_LIMIT:
            raise ValueError(f"حداکثر {RULE_LIMIT} قانون تخفیف مجاز است")
        rule = DiscountRule(shop_id=shop_id, enabled=True, used_count=0, name=data.name)
        session.add(rule)
    else:
        rule = await _own_rule(session, shop_id, rule_id)
    for field in fields(RuleInput):
        setattr(rule, field.name, getattr(data, field.name))
    rule.name = data.name.strip()
    await session.commit()
    return rule


async def set_rule_enabled(session: AsyncSession, shop_id: int | None, rule_id: int, enabled: bool) -> None:
    rule = await _own_rule(session, shop_id, rule_id)
    rule.enabled = bool(enabled)
    await session.commit()


async def delete_rule(session: AsyncSession, shop_id: int | None, rule_id: int) -> None:
    """Orders already discounted keep their price and ledger line; only future orders change."""
    await _own_rule(session, shop_id, rule_id)
    await session.execute(delete(DiscountRule).where(DiscountRule.id == int(rule_id), _shop_scope(shop_id)))
    await session.commit()


@dataclass(frozen=True, slots=True)
class RuleView:
    """What the rules page shows beside a stored rule."""

    percent: str
    remaining_days: int | None
    is_expired: bool


def rule_view(rule: DiscountRule, *, now: datetime) -> RuleView:
    ends = rule.ends_at
    if ends is not None and ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    remaining = None if ends is None else max(0, math.ceil((ends - now).total_seconds() / 86400))
    return RuleView(
        percent=f"{int(rule.percent_bps or 0) / (BPS // 100):g}",
        remaining_days=remaining,
        is_expired=ends is not None and ends <= now,
    )
