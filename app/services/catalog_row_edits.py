"""Rename and recolor the shop's categories and plans from the bot's menu editor.

Category and plan buttons are rows of the catalog, not settings: a change here is saved on the row
and shows wherever that category or plan is listed (the shop, the plan lists, the web panel).
Every call takes the catalog staff principal and re-checks that the row belongs to its shop: the
platform owner edits platform rows, a shop edits only its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, PlanCategory
from app.services.button_styles import normalize_style
from app.services.custom_plans import exclude_custom_order_plans
from app.services.plan_categories import (
    AUDIENCE_USERS,
    apply_category_owner_filter,
    get_owned_category,
    update_category,
)
from app.services.plans_catalog import apply_catalog_owner_filter, get_owned_plan
from app.services.shop_scope import ShopScopeError

CATALOG_ROWS_LIMIT = 30
NAME_MAX_LENGTH = 128
# Callback codes for a row color; "i" leaves the row without its own color (it inherits).
COLOR_CODES: dict[str, str | None] = {"w": "", "p": "primary", "s": "success", "d": "danger", "i": None}


@dataclass(slots=True)
class CatalogRows:
    categories: list[PlanCategory]
    plans: list[Plan]


async def editable_rows(session: AsyncSession, staff: dict) -> CatalogRows:
    """The shop's customer categories and active plans, as the shop lists them, bounded."""
    categories = select(PlanCategory).where(PlanCategory.audience == AUDIENCE_USERS)
    categories = apply_category_owner_filter(categories, staff).order_by(PlanCategory.sort_order, PlanCategory.id)
    plans = select(Plan).where(Plan.is_active.is_(True), Plan.is_trial.is_(False))
    plans = exclude_custom_order_plans(apply_catalog_owner_filter(plans, staff)).order_by(Plan.sort_order, Plan.id)
    return CatalogRows(
        categories=list((await session.scalars(categories.limit(CATALOG_ROWS_LIMIT))).all()),
        plans=list((await session.scalars(plans.limit(CATALOG_ROWS_LIMIT))).all()),
    )


async def owned_category(session: AsyncSession, staff: dict, category_id: int) -> PlanCategory:
    category = await get_owned_category(session, int(category_id), staff)
    if category is None or category.audience != AUDIENCE_USERS:
        raise ShopScopeError("دسته‌بندی یافت نشد")
    return category


async def owned_plan(session: AsyncSession, staff: dict, plan_id: int) -> Plan:
    plan = await get_owned_plan(session, int(plan_id), staff)
    if plan is None:
        raise ShopScopeError("پلن یافت نشد")
    return plan


def color_from_code(code: str) -> str | None:
    if code not in COLOR_CODES:
        raise ValueError("رنگ نامعتبر است")
    return COLOR_CODES[code]


async def rename_category(
    session: AsyncSession, staff: dict, category_id: int, *, name: str, icon_custom_emoji_id: str | None
) -> PlanCategory:
    """New name; a premium emoji sent with it becomes the icon, and the current icon stays otherwise."""
    await owned_category(session, staff, category_id)
    icon = icon_custom_emoji_id if icon_custom_emoji_id else ...
    return await update_category(session, staff, int(category_id), name=name, icon_custom_emoji_id=icon)


async def clear_category_icon(session: AsyncSession, staff: dict, category_id: int) -> PlanCategory:
    await owned_category(session, staff, category_id)
    return await update_category(session, staff, int(category_id), icon_custom_emoji_id=None)


async def set_category_color(session: AsyncSession, staff: dict, category_id: int, code: str) -> PlanCategory:
    color = color_from_code(code)
    await owned_category(session, staff, category_id)
    return await update_category(
        session, staff, int(category_id), button_style="inherit" if color is None else color
    )


async def rename_plan(session: AsyncSession, staff: dict, plan_id: int, *, name: str) -> Plan:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("نام پلن الزامی است")
    if len(cleaned) > NAME_MAX_LENGTH:
        raise ValueError("نام پلن خیلی طولانی است")
    plan = await owned_plan(session, staff, plan_id)
    plan.name = cleaned
    await session.commit()
    return plan


async def set_plan_color(session: AsyncSession, staff: dict, plan_id: int, code: str) -> Plan:
    """A plan's own color; ``i`` drops it so the plan takes its category's (or its kind's) color."""
    color = color_from_code(code)
    plan = await owned_plan(session, staff, plan_id)
    plan.button_style = None if color is None else normalize_style(color)
    await session.commit()
    return plan
