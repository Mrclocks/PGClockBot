"""Order and columns of the shop's category and plan lists, from the bot's menu editor.

The lists are the shop's own catalog rows, so their order is the rows' ``sort_order``, the same
field the web panel's plan and category forms set: a move in the bot shows in the web and the
other way round. A list is named by a short token that also travels in callback data:
``k`` the shop's categories, ``f`` every fixed plan, ``t`` the trial plans, ``n`` plans without a
category, ``c<id>`` one category's plans, ``w`` the wholesale list (every fixed plan).

Listing reads the catalog the way the shop shows it to customers. Moving re-reads the rows under
the catalog staff's owner filter with a row lock, so a shop can only reorder its own rows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, PlanCategory
from app.services.menu_rows import TEXT_LAYOUT_CHANGED, TEXT_ORDER_EDGE, columns_setting_key, fingerprint
from app.services.orders import list_active_plans
from app.services.plan_categories import apply_category_owner_filter, shop_categories_for_menu
from app.services.plans_catalog import apply_catalog_owner_filter
from app.services.screen_editor import EditorActor
from app.services.shop_scope import ShopScopeError
from app.services.users import set_setting

# Keeps the edit panel well under Telegram's 100 buttons; longer lists are ordered in the web panel.
CATALOG_LIST_LIMIT = 24
LIST_TOKEN = re.compile(r"k|f|t|n|w|c\d{1,10}")
CATALOG_SCREENS = ("c.shop", "c.shop.plans", "c.shop.wholesale")
COLUMN_CHOICES = ("", "1", "2")
TEXT_NOT_IN_SHOP = "این ردیف در فهرست فروشگاه شما نیست؛ منو را دوباره باز کنید."


@dataclass(frozen=True, slots=True)
class CatalogItem:
    kind: str  # "c" category, "p" plan
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class CatalogList:
    token: str
    items: tuple[CatalogItem, ...]
    title: str = ""
    # True when the list is longer than the editor shows.
    truncated: bool = False
    # The shop's category rows for the shop menu (``k``), to build that menu as customers see it.
    categories: tuple[Any, ...] = field(default=(), compare=False, repr=False)

    @property
    def state(self) -> str:
        return fingerprint([f"{item.kind}{item.id}" for item in self.items])


def screen_for_token(token: str) -> str:
    return {"k": "c.shop", "w": "c.shop.wholesale"}.get(token, "c.shop.plans")


def default_token(screen_id: str) -> str:
    return {"c.shop": "k", "c.shop.wholesale": "w"}.get(screen_id, "f")


def is_list_token(token: str) -> bool:
    return bool(LIST_TOKEN.fullmatch(token or ""))


_TITLES = {"k": "دسته‌ها", "f": "پلن‌های ثابت", "t": "پلن‌های تست", "n": "سایر", "w": "فروش عمده"}


async def _list_title(session: AsyncSession, token: str, scope: int | None) -> str:
    if not token.startswith("c"):
        return _TITLES[token]
    # Only a category of this shop names the list; another shop's id gets the generic title.
    owner = PlanCategory.owner_reseller_id == scope if scope else PlanCategory.owner_reseller_id.is_(None)
    name = await session.scalar(select(PlanCategory.name).where(PlanCategory.id == int(token[1:]), owner))
    return f"دستهٔ {name}" if name else "یک دسته"


def _plans_for(token: str, plans: Sequence[Plan]) -> list[Plan]:
    if token == "t":
        return [p for p in plans if p.is_trial]
    fixed = [p for p in plans if not p.is_trial]
    if token == "n":
        return [p for p in fixed if p.category_id is None]
    if token.startswith("c"):
        category_id = int(token[1:])
        return [p for p in fixed if p.category_id is not None and int(p.category_id) == category_id]
    return fixed


async def catalog_list(session: AsyncSession, *, shop_id: int | None, token: str) -> CatalogList:
    """The categories or plans of one shop list, in the order customers see them."""
    if not is_list_token(token):
        raise ValueError(TEXT_LAYOUT_CHANGED)
    # None is the platform catalog: the owner's editor only runs on the platform bot, where the
    # request has no shop. A shop always passes its own id.
    scope = int(shop_id) if shop_id else None
    plans = await list_active_plans(session, include_trial=True, reseller_id=scope)
    categories: list[PlanCategory] = []
    if token == "k":
        fixed = [p for p in plans if not p.is_trial]
        categories = await shop_categories_for_menu(session, fixed_plans=fixed, reseller_id=scope)
        items = [CatalogItem("c", int(c.id), c.name) for c in categories]
    else:
        items = [CatalogItem("p", int(p.id), p.name) for p in _plans_for(token, plans)]
    return CatalogList(
        token,
        tuple(items[:CATALOG_LIST_LIMIT]),
        title=await _list_title(session, token, scope),
        truncated=len(items) > CATALOG_LIST_LIMIT,
        categories=tuple(categories),
    )


def _moved_ids(items: Sequence[CatalogItem], index: int, target: int) -> list[int]:
    if abs(index - target) != 1 or not 0 <= index < len(items):
        raise ValueError(TEXT_LAYOUT_CHANGED)
    if not 0 <= target < len(items):
        raise ValueError(TEXT_ORDER_EDGE)
    ids = [item.id for item in items]
    ids[index], ids[target] = ids[target], ids[index]
    return ids


async def move_catalog_item(
    session: AsyncSession, staff: dict, *, listing: CatalogList, index: int, target: int
) -> None:
    """Move one row of ``listing`` to the next slot by rewriting the rows' ``sort_order``.

    The rows keep the slots they held in the catalog's order (their own sort values); only who
    holds which slot changes. Equal sort values are first spread so the move is visible.
    """
    new_ids = _moved_ids(listing.items, index, target)
    kind = listing.items[0].kind
    model = PlanCategory if kind == "c" else Plan
    query = select(model).where(model.id.in_(new_ids)).with_for_update()
    query = apply_category_owner_filter(query, staff) if kind == "c" else apply_catalog_owner_filter(query, staff)
    rows = {int(row.id): row for row in (await session.scalars(query)).all()}
    if len(rows) != len(new_ids):
        await session.rollback()
        raise ShopScopeError(TEXT_NOT_IN_SHOP)
    slots = sorted(int(rows[item.id].sort_order or 0) for item in listing.items)
    if len(set(slots)) != len(slots):
        slots = [slots[0] + offset for offset in range(len(slots))]
    for slot, row_id in zip(slots, new_ids, strict=True):
        rows[row_id].sort_order = slot
    await session.commit()


async def save_catalog_columns(session: AsyncSession, actor: EditorActor, *, screen_id: str, value: str) -> None:
    if screen_id not in CATALOG_SCREENS or value not in COLUMN_CHOICES or "customer" not in actor.audiences:
        raise ValueError(TEXT_LAYOUT_CHANGED)
    await set_setting(session, columns_setting_key(screen_id), value, reseller_id=actor.settings_scope)
