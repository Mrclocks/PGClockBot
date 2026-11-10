"""Managing a shop's cashback: who may, its rules and its settings.

Every read and write is scoped by the shop resolved from the signed-in staff
(``cashback_scope``), never by a form field, so one shop cannot see or change another's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CashbackBudget, CashbackRule
from app.services.authz import authz_from_staff, can_shop
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.users import get_setting, set_setting

CASHBACK_PERMISSION = "cashback"
RULE_LIMIT = 50
MAX_MONEY = 2_000_000_000
EVENTS = ("new", "renew", "addon")
COMBINE_MODES = ("best", "sum")


def cashback_scope(staff: Mapping[str, Any]) -> int | None:
    """Shop whose cashback ``staff`` manages (``None`` = platform); raises ``PermissionError``."""
    if is_platform_admin(dict(staff)):
        return None
    if not can_shop(authz_from_staff(staff), CASHBACK_PERMISSION):
        raise PermissionError("دسترسی مدیریت کش‌بک ندارید")
    shop_id = shop_owner_id(dict(staff))
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


@dataclass(frozen=True, slots=True)
class RuleInput:
    name: str
    mode: str
    percent_bps: int
    fixed_amount: int
    max_per_order: int | None
    min_order_amount: int
    events: str
    first_purchase_only: bool
    allow_with_discount: bool
    customer_max_count: int | None
    customer_max_amount: int | None
    priority: int


@dataclass(frozen=True, slots=True)
class CashbackSettings:
    enabled: bool
    combine: str
    hold_hours: int
    monthly_budget: int


def _shop_scope(shop_id: int | None):
    return CashbackRule.shop_id.is_(None) if shop_id is None else CashbackRule.shop_id == int(shop_id)


async def list_rules(session: AsyncSession, shop_id: int | None) -> list[CashbackRule]:
    return list((await session.scalars(
        select(CashbackRule).where(_shop_scope(shop_id)).order_by(CashbackRule.priority.desc(), CashbackRule.id)
        .limit(RULE_LIMIT)
    )).all())


async def _own_rule(session: AsyncSession, shop_id: int | None, rule_id: int) -> CashbackRule:
    rule = await session.scalar(select(CashbackRule).where(CashbackRule.id == int(rule_id), _shop_scope(shop_id)))
    if rule is None:
        raise ValueError("قانون کش‌بک یافت نشد")
    return rule


def _apply(rule: CashbackRule, data: RuleInput) -> None:
    for field in RuleInput.__dataclass_fields__:
        setattr(rule, field, getattr(data, field))


async def save_rule(session: AsyncSession, shop_id: int | None, data: RuleInput, *, rule_id: int | None = None) -> CashbackRule:
    if rule_id is None:
        if len(await list_rules(session, shop_id)) >= RULE_LIMIT:
            raise ValueError(f"حداکثر {RULE_LIMIT} قانون کش‌بک مجاز است")
        rule = CashbackRule(shop_id=shop_id, enabled=True, name=data.name)
        session.add(rule)
    else:
        rule = await _own_rule(session, shop_id, rule_id)
    _apply(rule, data)
    await session.commit()
    return rule


async def set_rule_enabled(session: AsyncSession, shop_id: int | None, rule_id: int, enabled: bool) -> None:
    rule = await _own_rule(session, shop_id, rule_id)
    rule.enabled = bool(enabled)
    await session.commit()


async def delete_rule(session: AsyncSession, shop_id: int | None, rule_id: int) -> None:
    """Granted cashback keeps its own numbers, so deleting a rule changes no grant."""
    await _own_rule(session, shop_id, rule_id)
    await session.execute(delete(CashbackRule).where(CashbackRule.id == int(rule_id), _shop_scope(shop_id)))
    await session.commit()


async def load_settings(session: AsyncSession, shop_id: int | None) -> CashbackSettings:
    async def read(key: str, default: str) -> str:
        return str(await get_setting(session, key, default, reseller_id=shop_id) or default)

    def number(raw: str, default: int) -> int:
        try:
            return int(raw)
        except ValueError:
            return default

    combine = await read("cashback_combine", "best")
    return CashbackSettings(
        enabled=(await read("cashback_enabled", "0")).lower() in {"1", "true", "on", "yes"},
        combine=combine if combine in COMBINE_MODES else "best",
        hold_hours=number(await read("cashback_hold_hours", "24"), 24),
        monthly_budget=number(await read("cashback_monthly_budget", "0"), 0),
    )


async def save_settings(session: AsyncSession, shop_id: int | None, settings: CashbackSettings) -> None:
    await set_setting(session, "cashback_enabled", "1" if settings.enabled else "0", reseller_id=shop_id)
    await set_setting(session, "cashback_combine", settings.combine, reseller_id=shop_id)
    await set_setting(session, "cashback_hold_hours", str(settings.hold_hours), reseller_id=shop_id)
    await set_setting(session, "cashback_monthly_budget", str(settings.monthly_budget), reseller_id=shop_id)
    await session.commit()


async def budget_used(session: AsyncSession, shop_id: int | None, *, now: datetime | None = None) -> int:
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    used = await session.scalar(select(CashbackBudget.used).where(
        CashbackBudget.shop_key == int(shop_id or 0), CashbackBudget.month == month))
    return int(used or 0)
