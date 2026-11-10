"""Customer service addon packs (extra GB / days) — shop-scoped, fail-closed."""

from __future__ import annotations

import logging
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog_pricing import annotate_prices, assert_expected_price, quote_product
from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    Plan,
    ServiceAddonPack,
    UserService,
    ResellerCharge,
)
from app.services.formatting import (
    format_bytes,
    format_expire_short,
    hold_duration_from_info,
    is_on_hold_status,
    parse_expire,
)
from app.services.numbers import to_fa_digits
from app.services.plans_catalog import require_catalog_owner_id
from app.services.payg_charges import new_request_key
from app.services.payg_gate import (
    SHORTFALL_FOR_CUSTOMER,
    assert_shop_can_afford,
    charge_shop_change,
    finish_charge,
    settle_failed_charge,
    shop_charges_at_creation,
)
from app.services.payg_pricing import ChargeEvent, ServiceChange
from app.services.provision_gate import ProvisionError, hold_limit_change, new_hold_key
from app.services.service_live_info import validate_service_info
from app.services.volume_allocation import (
    HeldVolume,
    VolumeAllocationError,
    assert_volume_delta_allowed,
    is_hard_panel_rejection,
    release_volume_hold,
)
from app.services.shop_scope import ShopScopeError, is_platform_admin, shop_owner_id

log = logging.getLogger(__name__)

KIND_VOLUME = "volume"
KIND_DURATION = "duration"
VALID_KINDS = frozenset({KIND_VOLUME, KIND_DURATION})
ADDON_PREVIEW_NOTICE = (
    "پیش‌نمایش بر اساس مانده فعلی است؛ مصرف و گذشت زمان تا اجرای افزایش از مانده کم می‌شود."
)

# note: svc_addon:{pack_id}:{service_id}[:{kind}:{amount}]
# kind/amount snapshot freezes entitlement at order time (price is on order.amount).
_NOTE_RE = re.compile(
    r"^svc_addon:(\d+):(\d+)(?::(volume|duration):([0-9]+(?:\.[0-9]+)?))?$"
)

MAX_VOLUME_GB = 10_000.0
MAX_DURATION_DAYS = 3650
MAX_PRICE = 2_000_000_000


def format_addon_note(
    pack_id: int,
    service_id: int,
    *,
    kind: str,
    amount: float,
) -> str:
    """Server-written order note with snapshotted kind/amount."""
    k = (kind or "").strip().lower()
    if k not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    amt = float(amount)
    if k == KIND_DURATION:
        amt_s = str(int(amt))
    else:
        # Avoid :g scientific notation (e.g. 1e+06) which breaks _NOTE_RE.
        if amt == int(amt) and abs(amt) < 1e15:
            amt_s = str(int(amt))
        else:
            amt_s = format(amt, ".10f").rstrip("0").rstrip(".")
            if not amt_s or amt_s == "-":
                amt_s = "0"
    return f"svc_addon:{int(pack_id)}:{int(service_id)}:{k}:{amt_s}"


def parse_addon_note(
    note: str | None,
) -> tuple[int, int, str | None, float | None] | None:
    """Return (pack_id, service_id, kind|None, amount|None). Legacy notes omit snapshot."""
    m = _NOTE_RE.match((note or "").strip())
    if not m:
        return None
    pack_id = int(m.group(1))
    service_id = int(m.group(2))
    kind = m.group(3)
    amt_raw = m.group(4)
    if kind and amt_raw is not None:
        return pack_id, service_id, kind, float(amt_raw)
    return pack_id, service_id, None, None


def apply_pack_owner_filter(query, staff: dict | None):
    if is_platform_admin(staff):
        return query.where(ServiceAddonPack.owner_reseller_id.is_(None))
    if not staff:
        return query.where(ServiceAddonPack.owner_reseller_id.is_(None))
    rid = shop_owner_id(staff)
    if not rid:
        return query.where(ServiceAddonPack.id < 0)
    return query.where(ServiceAddonPack.owner_reseller_id == rid)


def pack_belongs_to_staff(pack: ServiceAddonPack | None, staff: dict | None) -> bool:
    if not pack:
        return False
    if is_platform_admin(staff):
        return pack.owner_reseller_id is None
    if not staff:
        return pack.owner_reseller_id is None
    rid = shop_owner_id(staff)
    if not rid:
        return False
    return int(pack.owner_reseller_id or 0) == int(rid)


def pack_matches_shop(pack: ServiceAddonPack | None, shop_rid: int | None) -> bool:
    if not pack or not pack.is_active:
        return False
    if shop_rid:
        return int(pack.owner_reseller_id or 0) == int(shop_rid)
    return pack.owner_reseller_id is None


def category_ids_equal(a: int | None, b: int | None) -> bool:
    """Strict category match — both unset, or the same positive id."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return int(a) == int(b)


def pack_matches_service_category(
    pack: ServiceAddonPack | None, service_category_id: int | None
) -> bool:
    """A pack without a category fits every category of its shop; a categorised pack only its own.

    «Without a category» is decided by ``category_id`` alone, so a pack whose category was
    removed (or whose ``is_general`` flag was never set) is still offered everywhere instead of
    vanishing. Cross-category purchase of a category pack stays fail-closed.
    Plan-scoped packs are not judged here — use ``pack_matches_service``.
    """
    if not pack:
        return False
    if getattr(pack, "plan_id", None) is not None:
        return False
    if pack.is_general or pack.category_id is None:
        return True
    return category_ids_equal(
        int(pack.category_id),
        int(service_category_id) if service_category_id is not None else None,
    )


def pack_matches_service(
    pack: ServiceAddonPack | None,
    *,
    service_plan_id: int | None,
    service_category_id: int | None,
    plan_catalog_exclusive: bool = False,
) -> bool:
    """Whether ``pack`` may be bought for a service of this plan/category.

    When ``plan_catalog_exclusive`` is true for the pack's *kind* (the sales plan
    has plan-scoped rows of that kind), only a pack whose ``plan_id`` equals
    ``service_plan_id`` matches — general and category packs of that kind are
    refused even if a client posts their id directly. Other kinds stay on the
    legacy category/general path.
    """
    if not pack:
        return False
    pack_plan = getattr(pack, "plan_id", None)
    if pack_plan is not None:
        return service_plan_id is not None and int(pack_plan) == int(service_plan_id)
    if plan_catalog_exclusive:
        return False
    return pack_matches_service_category(pack, service_category_id)


def prefer_category_packs(packs: list[ServiceAddonPack]) -> list[ServiceAddonPack]:
    """Drop a general pack when a category pack offers the same kind and amount.

    The category pack is the shop's price for that category, so customers never see
    two prices for the same bundle. The input order is kept.
    """
    specific = {(pack.kind, float(pack.amount)) for pack in packs if pack.category_id is not None}
    return [pack for pack in packs if pack.category_id is not None or (pack.kind, float(pack.amount)) not in specific]


async def service_plan_category_id(
    session: AsyncSession, service: UserService
) -> int | None:
    """Category of the sales plan linked to this service (None if unset/missing)."""
    if not service or not service.plan_id:
        return None
    plan = await session.get(Plan, int(service.plan_id))
    if not plan or plan.category_id is None:
        return None
    return int(plan.category_id)


def is_addon_order(order: Order | None) -> bool:
    return bool(order and parse_addon_note(order.note))


def kind_label(kind: str) -> str:
    return "حجم" if kind == KIND_VOLUME else "زمان"


def format_amount_label(kind: str, amount: float) -> str:
    if kind == KIND_VOLUME:
        amt = float(amount)
        if amt == int(amt):
            return f"{int(amt)} گیگ"
        return f"{amt:g} گیگ"
    days = int(float(amount))
    return f"{days} روز"


def amount_label(pack: ServiceAddonPack) -> str:
    return format_amount_label(pack.kind, float(pack.amount))


_PACK_BTN_NAME_MAX = 28


def _pack_name_redundant(name: str, amount: str) -> bool:
    """True when the pack name restates the amount (e.g. «۱۰ گیگ» vs «10 گیگ»)."""

    def norm(text: str) -> str:
        t = "".join(str(text).split()).replace("گیگابایت", "گیگ").lower()
        return to_fa_digits(t)

    n, a = norm(name), norm(amount)
    if not n or n == a:
        return True
    # Digits-only core equal (ignore unit words).
    n_digits = re.sub(r"[^\d۰-۹]", "", n)
    a_digits = re.sub(r"[^\d۰-۹]", "", a)
    return bool(n_digits) and n_digits == a_digits and (
        "گیگ" in n or "روز" in n or "گیگ" in a or "روز" in a
    )


def pack_purchase_button_text(pack: ServiceAddonPack, *, price_text: str) -> str:
    """Plain Telegram button caption: emoji, amount, price; skip a redundant name."""
    emoji = "📦" if pack.kind == KIND_VOLUME else "⏱"
    amount = amount_label(pack)
    raw_name = (pack.name or "").strip().replace("\n", " ").replace("\r", "")
    if raw_name and not _pack_name_redundant(raw_name, amount):
        name = raw_name if len(raw_name) <= _PACK_BTN_NAME_MAX else raw_name[: _PACK_BTN_NAME_MAX - 1] + "…"
        return f"{emoji} {name} · {amount} — {price_text}"
    return f"{emoji} {amount} — {price_text}"


async def list_packs(
    session: AsyncSession,
    staff: dict | None,
    *,
    active_only: bool = False,
    kind: str | None = None,
) -> list[ServiceAddonPack]:
    q = select(ServiceAddonPack).order_by(
        ServiceAddonPack.kind, ServiceAddonPack.sort_order, ServiceAddonPack.id
    )
    q = apply_pack_owner_filter(q, staff)
    if active_only:
        q = q.where(ServiceAddonPack.is_active.is_(True))
    if kind:
        if kind not in VALID_KINDS:
            raise ValueError("نوع بسته نامعتبر است")
        q = q.where(ServiceAddonPack.kind == kind)
    return list((await session.execute(q)).scalars().all())


async def list_shop_packs(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
    active_only: bool = True,
    kind: str | None = None,
    service_category_id: int | None | object = ...,
) -> list[ServiceAddonPack]:
    """List shop packs. Pass ``service_category_id`` to get what a service of that plan
    category may buy: the general packs plus the packs tied to that category (``None``
    means a service without a category, which only sees general packs).

    Omit it (default) to return every shop pack (admin lists).
    """
    from app.services.users import current_shop_reseller_id

    rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    q = select(ServiceAddonPack).order_by(
        ServiceAddonPack.kind, ServiceAddonPack.sort_order, ServiceAddonPack.id
    )
    if rid:
        q = q.where(ServiceAddonPack.owner_reseller_id == int(rid))
    else:
        q = q.where(ServiceAddonPack.owner_reseller_id.is_(None))
    if active_only:
        q = q.where(ServiceAddonPack.is_active.is_(True))
    if kind:
        q = q.where(ServiceAddonPack.kind == kind)
    if service_category_id is not ...:
        # Plan-scoped packs are offered only via list_packs_for_plan / list_packs_for_service.
        q = q.where(ServiceAddonPack.plan_id.is_(None))
        fits = or_(ServiceAddonPack.is_general.is_(True), ServiceAddonPack.category_id.is_(None))
        if service_category_id is not None:
            fits = or_(fits, ServiceAddonPack.category_id == int(service_category_id))
        q = q.where(fits)
    packs = list((await session.execute(q)).scalars().all())
    if service_category_id is not ...:
        packs = prefer_category_packs(packs)
    await annotate_prices(session, packs, shop_id=rid)
    return packs


async def list_packs_for_plan(
    session: AsyncSession,
    *,
    plan_id: int,
    reseller_id: int | None = None,
    active_only: bool = True,
    kind: str | None = None,
) -> list[ServiceAddonPack]:
    """Packs exclusively scoped to one sales plan (never shown on other plans)."""
    from app.services.users import current_shop_reseller_id

    rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    q = select(ServiceAddonPack).where(ServiceAddonPack.plan_id == int(plan_id)).order_by(
        ServiceAddonPack.kind, ServiceAddonPack.sort_order, ServiceAddonPack.id
    )
    if rid:
        q = q.where(ServiceAddonPack.owner_reseller_id == int(rid))
    else:
        q = q.where(ServiceAddonPack.owner_reseller_id.is_(None))
    if active_only:
        q = q.where(ServiceAddonPack.is_active.is_(True))
    if kind:
        q = q.where(ServiceAddonPack.kind == kind)
    packs = list((await session.execute(q)).scalars().all())
    await annotate_prices(session, packs, shop_id=rid)
    return packs


async def plan_has_scoped_packs(
    session: AsyncSession,
    *,
    plan_id: int,
    reseller_id: int | None = None,
    kind: str | None = None,
) -> bool:
    """True when the plan has at least one plan-scoped pack row (active or not).

    Pass ``kind`` (volume/duration) to scope the check to that kind only — exclusivity
    is per kind so a plan with only volume rows still offers general duration packs.
    """
    from app.services.users import current_shop_reseller_id

    rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    q = select(ServiceAddonPack.id).where(ServiceAddonPack.plan_id == int(plan_id)).limit(1)
    if kind is not None:
        if kind not in VALID_KINDS:
            raise ValueError("نوع بسته نامعتبر است")
        q = q.where(ServiceAddonPack.kind == kind)
    if rid:
        q = q.where(ServiceAddonPack.owner_reseller_id == int(rid))
    else:
        q = q.where(ServiceAddonPack.owner_reseller_id.is_(None))
    return (await session.scalar(q)) is not None


async def list_packs_for_service(
    session: AsyncSession,
    service: UserService,
    *,
    kind: str | None = None,
    active_only: bool = True,
) -> list[ServiceAddonPack]:
    """Packs purchasable for one service.

    Exclusivity is per kind: if the sales plan has any plan-scoped rows of kind X
    (even inactive), only that plan's active X packs are offered for X. Kind Y
    without scoped rows still uses the legacy category/general catalog.
    """
    from app.services.service_automation import service_shop_id

    shop_rid = await service_shop_id(session, service)
    plan_id = int(service.plan_id) if getattr(service, "plan_id", None) else None
    cat_id = await service_plan_category_id(session, service)

    async def _for_kind(k: str) -> list[ServiceAddonPack]:
        if plan_id and await plan_has_scoped_packs(
            session, plan_id=plan_id, reseller_id=shop_rid, kind=k
        ):
            return await list_packs_for_plan(
                session,
                plan_id=plan_id,
                reseller_id=shop_rid,
                active_only=active_only,
                kind=k,
            )
        return await list_shop_packs(
            session,
            reseller_id=shop_rid,
            active_only=active_only,
            kind=k,
            service_category_id=cat_id,
        )

    if kind is not None:
        if kind not in VALID_KINDS:
            raise ValueError("نوع بسته نامعتبر است")
        return await _for_kind(kind)
    out: list[ServiceAddonPack] = []
    for k in (KIND_VOLUME, KIND_DURATION):
        out.extend(await _for_kind(k))
    return out


async def available_addon_kinds_for_service(
    session: AsyncSession,
    service: UserService,
) -> list[str]:
    """Kinds that currently have at least one active purchasable pack for ``service``."""
    kinds: list[str] = []
    for k in (KIND_VOLUME, KIND_DURATION):
        if await list_packs_for_service(session, service, kind=k, active_only=True):
            kinds.append(k)
    return kinds


async def get_owned_pack(
    session: AsyncSession, pack_id: int, staff: dict | None
) -> ServiceAddonPack | None:
    pack = await session.get(ServiceAddonPack, int(pack_id))
    if not pack_belongs_to_staff(pack, staff):
        return None
    return pack


def _validate_pack_fields(
    *,
    name: str,
    kind: str,
    amount: float,
    price: int,
    description: str | None,
) -> tuple[str, str, float, int, str | None]:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("نام بسته الزامی است")
    if len(cleaned) > 128:
        raise ValueError("نام بسته خیلی طولانی است")
    k = (kind or "").strip().lower()
    if k not in VALID_KINDS:
        raise ValueError("نوع بسته باید حجم یا زمان باشد")
    try:
        amt = float(amount)
    except (TypeError, ValueError) as e:
        raise ValueError("مقدار بسته نامعتبر است") from e
    if amt <= 0:
        raise ValueError("مقدار باید بزرگ‌تر از صفر باشد")
    if k == KIND_VOLUME and amt > MAX_VOLUME_GB:
        raise ValueError("حجم بیش از حد مجاز است")
    if k == KIND_DURATION:
        if amt != int(amt):
            raise ValueError("روز باید عدد صحیح باشد")
        if amt > MAX_DURATION_DAYS:
            raise ValueError("مدت بیش از حد مجاز است")
        amt = float(int(amt))
    try:
        price_i = int(price)
    except (TypeError, ValueError) as e:
        raise ValueError("قیمت نامعتبر است") from e
    if price_i < 0 or price_i > MAX_PRICE:
        raise ValueError("قیمت نامعتبر است")
    desc = (description or "").strip() or None
    if desc and len(desc) > 255:
        raise ValueError("توضیح خیلی طولانی است")
    return cleaned, k, amt, price_i, desc


MAX_PACK_ROWS = 10
_PACK_UNITS = {KIND_VOLUME: "گیگ", KIND_DURATION: "روز"}


@dataclass(frozen=True, slots=True)
class PackRow:
    """One pack of a batch: what it adds, its price and its button colour (None = inherit)."""

    amount: float
    price: int
    button_style: str | None = None


def default_pack_name(kind: str, amount: float) -> str:
    """Name shown in the bot when the admin leaves it blank, e.g. «۱۰ گیگ»."""
    number = int(amount) if float(amount).is_integer() else amount
    return f"{to_fa_digits(str(number))} {_PACK_UNITS.get(kind, '')}".strip()


def _pack_style(button_style: str | None | object) -> str | None:
    from app.services.button_styles import normalize_style

    if button_style is ... or button_style is None:
        return None
    if str(button_style).strip().lower() in {"", "inherit", "__inherit__"}:
        return None
    return normalize_style(str(button_style))


async def _resolve_pack_scope(
    session: AsyncSession,
    staff: dict | None,
    owner_id: int | None,
    *,
    plan_id: int | None,
    category_id: int | None | object,
) -> tuple[int | None, int | None]:
    """(plan id, category id) a new pack is tied to, checked against the caller's shop."""
    from app.services.plan_categories import AUDIENCE_USERS, resolve_category_for_plan_write

    if plan_id is not None:
        plan = await session.get(Plan, int(plan_id))
        if plan is None:
            raise ValueError("پلن یافت نشد")
        plan_owner = getattr(plan, "owner_reseller_id", None)
        if owner_id is None and plan_owner is not None:
            raise ValueError("پلن متعلق به این فروشگاه نیست")
        if owner_id is not None and int(plan_owner or 0) != int(owner_id):
            raise ValueError("پلن متعلق به این فروشگاه نیست")
        return int(plan.id), None
    if category_id is ...:
        return None, None
    category = await resolve_category_for_plan_write(
        session, staff, category_id, expected_audience=AUDIENCE_USERS
    )
    return None, category


async def create_packs(
    session: AsyncSession,
    staff: dict | None,
    *,
    kind: str,
    rows: list[PackRow],
    name: str = "",
    description: str | None = None,
    sort_order: int = 0,
    category_id: int | None | object = ...,
    plan_id: int | None = None,
) -> list[ServiceAddonPack]:
    """Add several packs of one kind and scope in one commit; nothing is saved if one row is invalid.

    A blank ``name`` gives each pack a name from its amount; a typed name needs a single row.
    """
    if not rows:
        raise ValueError("حداقل یک بسته وارد کنید")
    if len(rows) > MAX_PACK_ROWS:
        raise ValueError(f"حداکثر {to_fa_digits(str(MAX_PACK_ROWS))} بسته در هر بار")
    typed_name = (name or "").strip()
    if typed_name and len(rows) > 1:
        raise ValueError("برای چند بسته نام را خالی بگذارید؛ نام هر بسته از مقدارش ساخته می‌شود")
    owner_id = require_catalog_owner_id(staff)
    validated = [
        _validate_pack_fields(
            name=typed_name or default_pack_name((kind or "").strip().lower(), row.amount),
            kind=kind,
            amount=row.amount,
            price=row.price,
            description=description,
        )
        for row in rows
    ]
    resolved_plan, resolved_cat = await _resolve_pack_scope(
        session, staff, owner_id, plan_id=plan_id, category_id=category_id
    )
    packs = [
        ServiceAddonPack(
            name=cleaned,
            kind=k,
            amount=amt,
            price=price_i,
            description=desc,
            owner_reseller_id=owner_id,
            category_id=resolved_cat,
            plan_id=resolved_plan,
            button_style=_pack_style(row.button_style),
            is_general=resolved_plan is None and resolved_cat is None,
            sort_order=int(sort_order or 0),
            is_active=True,
        )
        for (cleaned, k, amt, price_i, desc), row in zip(validated, rows, strict=True)
    ]
    session.add_all(packs)
    await session.commit()
    for pack in packs:
        await session.refresh(pack)
    return packs


async def create_pack(
    session: AsyncSession,
    staff: dict | None,
    *,
    name: str,
    kind: str,
    amount: float,
    price: int,
    description: str | None = None,
    sort_order: int = 0,
    category_id: int | None | object = ...,
    plan_id: int | None = None,
    button_style: str | None | object = ...,
) -> ServiceAddonPack:
    if not (name or "").strip():
        raise ValueError("نام بسته الزامی است")
    style = None if button_style is ... else button_style
    packs = await create_packs(
        session,
        staff,
        kind=kind,
        rows=[PackRow(amount=amount, price=price, button_style=style)],
        name=name,
        description=description,
        sort_order=sort_order,
        category_id=category_id,
        plan_id=plan_id,
    )
    return packs[0]


async def update_pack(
    session: AsyncSession,
    staff: dict | None,
    pack_id: int,
    *,
    name: str | None = None,
    kind: str | None = None,
    amount: float | None = None,
    price: int | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
    category_id: int | None | object = ...,
) -> ServiceAddonPack:
    from app.services.plan_categories import (
        AUDIENCE_USERS,
        resolve_category_for_plan_write,
    )

    pack = await get_owned_pack(session, pack_id, staff)
    if not pack:
        raise ShopScopeError("بسته یافت نشد")
    cleaned, k, amt, price_i, desc = _validate_pack_fields(
        name=name if name is not None else pack.name,
        kind=kind if kind is not None else pack.kind,
        amount=amount if amount is not None else pack.amount,
        price=price if price is not None else pack.price,
        description=description if description is not None else (pack.description or ""),
    )
    pack.name = cleaned
    pack.kind = k
    pack.amount = amt
    pack.price = price_i
    if description is not None:
        pack.description = desc
    if sort_order is not None:
        pack.sort_order = int(sort_order)
    if is_active is not None:
        pack.is_active = bool(is_active)
    if category_id is not ...:
        pack.category_id = await resolve_category_for_plan_write(
            session,
            staff,
            category_id,
            expected_audience=AUDIENCE_USERS,
            allow_inactive_id=pack.category_id,
        )
        pack.is_general = pack.category_id is None
    await session.commit()
    await session.refresh(pack)
    return pack


async def delete_pack(
    session: AsyncSession, staff: dict | None, pack_id: int
) -> None:
    pack = await get_owned_pack(session, pack_id, staff)
    if not pack:
        raise ShopScopeError("بسته یافت نشد")
    await session.delete(pack)
    await session.commit()


MAX_PLAN_SCOPED_PACKS_PER_KIND = 20
DAYS_PER_EXTRA_MONTH = 30
MAX_EXTRA_MONTHS = 120


def _form_getlist(form: object, key: str) -> list[str]:
    if hasattr(form, "getlist"):
        return [str(v) for v in form.getlist(key)]  # type: ignore[attr-defined]
    raw = form.get(key) if hasattr(form, "get") else None  # type: ignore[attr-defined]
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(v) for v in raw]
    return [str(raw)]


def _parse_nonneg_int_field(raw: object, *, label: str) -> int:
    text = str(raw if raw is not None else "0").strip() or "0"
    try:
        value = int(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{label} نامعتبر است") from e
    if value < 0 or value > MAX_PRICE:
        raise ValueError(f"{label} نامعتبر است")
    return value


def apply_plan_extra_rates_from_form(plan: Plan, form: object) -> None:
    """Set plan.allow_buy_extra and unit rates from the fixed-plan form."""
    allow = bool(form.get("allow_buy_extra")) if hasattr(form, "get") else False  # type: ignore[attr-defined]
    plan.allow_buy_extra = allow
    if not allow:
        plan.extra_gb_price = 0
        plan.extra_month_price = 0
        return
    plan.extra_gb_price = _parse_nonneg_int_field(
        form.get("extra_gb_price") if hasattr(form, "get") else 0,  # type: ignore[attr-defined]
        label="قیمت هر گیگ",
    )
    plan.extra_month_price = _parse_nonneg_int_field(
        form.get("extra_month_price") if hasattr(form, "get") else 0,  # type: ignore[attr-defined]
        label="قیمت هر ماه",
    )


def plan_duration_months_for_form(pack: ServiceAddonPack) -> int:
    """Months shown in the editor for a stored duration pack (days ÷ 30)."""
    days = int(float(pack.amount or 0))
    if days <= 0:
        return 1
    months = max(1, round(days / DAYS_PER_EXTRA_MONTH))
    return min(MAX_EXTRA_MONTHS, months)


def _pack_style_from_raw(raw_style: object) -> str | None:
    from app.services.button_styles import normalize_style

    if raw_style is None or str(raw_style).strip().lower() in {"inherit", "__inherit__", ""}:
        return None
    return normalize_style(str(raw_style)) or None


async def replace_plan_scoped_packs_from_form(
    session: AsyncSession,
    staff: dict | None,
    *,
    plan_id: int,
    form: object,
) -> None:
    """Replace plan-scoped volume/duration stairs from the fixed user-plan form.

    Pack price = plan unit rate × amount (GB or months). Duration amounts are
    entered as months and stored as days (months × 30) for delivery. When
    ``allow_buy_extra`` is off, all plan-scoped packs are cleared. Does not commit.
    """
    from app.services.users import current_shop_reseller_id

    owner_id = require_catalog_owner_id(staff)
    plan = await session.get(Plan, int(plan_id))
    if plan is None:
        raise ValueError("پلن یافت نشد")
    plan_owner = getattr(plan, "owner_reseller_id", None)
    if owner_id is None and plan_owner is not None:
        raise ValueError("پلن متعلق به این فروشگاه نیست")
    if owner_id is not None and int(plan_owner or 0) != int(owner_id):
        raise ValueError("پلن متعلق به این فروشگاه نیست")

    rid = owner_id if owner_id is not None else current_shop_reseller_id()
    existing = await list_packs_for_plan(
        session, plan_id=int(plan_id), reseller_id=rid, active_only=False
    )
    for pack in existing:
        await session.delete(pack)

    if not bool(getattr(plan, "allow_buy_extra", False)):
        return

    gb_rate = max(0, int(getattr(plan, "extra_gb_price", 0) or 0))
    month_rate = max(0, int(getattr(plan, "extra_month_price", 0) or 0))

    vol_amounts = _form_getlist(form, "plan_vol_amount")
    vol_styles = _form_getlist(form, "plan_vol_style")
    if len(vol_amounts) > MAX_PLAN_SCOPED_PACKS_PER_KIND:
        raise ValueError(f"حداکثر {MAX_PLAN_SCOPED_PACKS_PER_KIND} بسته حجم مجاز است")
    for index, raw_amount in enumerate(vol_amounts):
        style = _pack_style_from_raw(
            vol_styles[index] if index < len(vol_styles) else "inherit"
        )
        price_i = 0
        # Price is derived after amount validation; pass a placeholder first.
        _tmp, k, amt, _price, _desc = _validate_pack_fields(
            name="بسته",
            kind=KIND_VOLUME,
            amount=raw_amount,
            price=0,
            description=None,
        )
        if amt != int(amt):
            raise ValueError("مقدار حجم باید عدد صحیح باشد")
        amt = float(int(amt))
        price_i = gb_rate * int(amt)
        if price_i > MAX_PRICE:
            raise ValueError("قیمت بسته حجم نامعتبر است")
        label = format_amount_label(k, amt)
        session.add(
            ServiceAddonPack(
                name=label,
                kind=k,
                amount=amt,
                price=price_i,
                description=None,
                owner_reseller_id=owner_id,
                category_id=None,
                plan_id=int(plan_id),
                button_style=style,
                is_general=False,
                sort_order=index,
                is_active=True,
            )
        )

    dur_amounts = _form_getlist(form, "plan_dur_amount")
    dur_styles = _form_getlist(form, "plan_dur_style")
    if len(dur_amounts) > MAX_PLAN_SCOPED_PACKS_PER_KIND:
        raise ValueError(f"حداکثر {MAX_PLAN_SCOPED_PACKS_PER_KIND} بسته زمان مجاز است")
    for index, raw_months in enumerate(dur_amounts):
        style = _pack_style_from_raw(
            dur_styles[index] if index < len(dur_styles) else "inherit"
        )
        try:
            months = int(str(raw_months).strip())
        except (TypeError, ValueError) as e:
            raise ValueError("مقدار ماه نامعتبر است") from e
        if months <= 0 or months > MAX_EXTRA_MONTHS:
            raise ValueError("مقدار ماه نامعتبر است")
        days = months * DAYS_PER_EXTRA_MONTH
        price_i = month_rate * months
        if price_i > MAX_PRICE:
            raise ValueError("قیمت بسته زمان نامعتبر است")
        _tmp, k, amt, price_ok, _desc = _validate_pack_fields(
            name=f"{months} ماه",
            kind=KIND_DURATION,
            amount=days,
            price=price_i,
            description=None,
        )
        session.add(
            ServiceAddonPack(
                name=_tmp,
                kind=k,
                amount=amt,
                price=price_ok,
                description=None,
                owner_reseller_id=owner_id,
                category_id=None,
                plan_id=int(plan_id),
                button_style=style,
                is_general=False,
                sort_order=index,
                is_active=True,
            )
        )


def pack_volume_bytes(amount: float) -> int:
    """Bytes a volume pack adds, with the rounding the panel write uses."""
    return int(float(amount) * (1024**3))


def addon_terms(pack: ServiceAddonPack) -> dict[str, Any]:
    """Frozen pack entitlement used to re-validate a Mini App preview at pay time."""
    if not pack or pack.kind not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    if pack.kind == KIND_VOLUME:
        bytes_added = pack_volume_bytes(pack.amount)
        if bytes_added <= 0:
            raise ValueError("حجم بسته نامعتبر است")
        return {"version": 1, "kind": KIND_VOLUME, "volume_bytes": bytes_added, "duration_seconds": 0}
    days = int(float(pack.amount))
    if days <= 0 or days != float(pack.amount):
        raise ValueError("مدت بسته نامعتبر است")
    return {"version": 1, "kind": KIND_DURATION, "volume_bytes": 0, "duration_seconds": days * 86400}


def _time_label(seconds: int | None) -> str:
    if seconds is None:
        return "نامحدود"
    days = f"{seconds / 86400:.2f}".rstrip("0").rstrip(".")
    return f"{days} روز"


def calculate_addon_preview(
    info: dict[str, Any],
    *,
    kind: str,
    amount: float,
    now: int | None = None,
) -> dict[str, Any]:
    """Before/after lines for a volume or duration pack — same arithmetic as apply."""
    info = validate_service_info(info)
    if info.get("error"):
        raise ValueError("اطلاعات زمان یا حجم سرویس کامل نیست؛ دوباره به‌روزرسانی کنید")
    if kind not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    now = int(time.time()) if now is None else int(now)
    warnings: list[str] = []
    on_hold = is_on_hold_status(info.get("status"))

    if kind == KIND_VOLUME:
        used = int(info["used_traffic"])
        old_limit = int(info["data_limit"] or 0)
        if old_limit <= 0:
            raise ValueError("این سرویس حجم نامحدود دارد — بسته حجم قابل خرید نیست")
        remaining = max(0, old_limit - used)
        added = pack_volume_bytes(amount)
        if added <= 0:
            raise ValueError("حجم نامعتبر است")
        new_remaining = remaining + added
        lines = [
            f"حجم مانده: {format_bytes(remaining)}",
            f"حجم بسته: {format_bytes(added)}",
            f"حجم پس از افزایش: {format_bytes(new_remaining)}",
        ]
        if on_hold:
            warnings.append("سرویس در انتظار اتصال است؛ پس از خرید فعال می‌شود.")
        return {
            "lines": lines,
            "warnings": warnings,
            "notice": ADDON_PREVIEW_NOTICE,
            "result_remaining_bytes": new_remaining,
            "result_seconds": None,
        }

    days = int(float(amount))
    if days <= 0:
        raise ValueError("مدت نامعتبر است")
    added_seconds = days * 86400
    old_expire = parse_expire(info.get("expire", info.get("expire_date")))
    if old_expire is not None and old_expire.tzinfo is None:
        from datetime import timezone as _tz

        old_expire = old_expire.replace(tzinfo=_tz.utc)
    expire_ts = int(old_expire.timestamp()) if old_expire else None
    if expire_ts is None and on_hold:
        old_seconds = hold_duration_from_info(info)
        if old_seconds is None:
            raise ValueError("مدت سرویس در انتظار مشخص نیست — بسته قابل اعمال نیست")
        new_seconds = old_seconds + added_seconds
        target_expire = None
        pending_start = True
    elif expire_ts is None:
        raise ValueError("این سرویس زمان نامحدود دارد — بسته زمان قابل خرید نیست")
    else:
        old_seconds = max(0, expire_ts - now)
        base = max(now, expire_ts)
        target_expire = base + added_seconds
        new_seconds = max(0, target_expire - now)
        pending_start = False
    lines = [
        f"زمان مانده: {_time_label(old_seconds)}",
        f"زمان بسته: {_time_label(added_seconds)}",
        f"زمان پس از افزایش: {_time_label(new_seconds)}"
        + (" پس از اتصال" if pending_start else ""),
    ]
    if target_expire:
        lines.append(f"انقضای جدید: {format_expire_short(target_expire)}")
    if on_hold and not pending_start:
        warnings.append("سرویس در انتظار اتصال است؛ پس از خرید فعال می‌شود.")
    return {
        "lines": lines,
        "warnings": warnings,
        "notice": ADDON_PREVIEW_NOTICE,
        "result_remaining_bytes": None,
        "result_seconds": new_seconds,
    }


async def validate_addon_selection(
    session: AsyncSession,
    *,
    user_id: int,
    service: UserService,
    pack: ServiceAddonPack,
    shop_id: int | None,
) -> dict[str, Any]:
    """Shared ownership / shop / plan-scope / quota gates for preview and pay."""
    if not pack or not pack.is_active:
        raise ValueError("بسته یافت نشد")
    if pack.kind not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    if not pack_matches_shop(pack, shop_id):
        raise ValueError("این بسته در این فروشگاه موجود نیست")
    if int(service.bot_user_id) != int(user_id):
        raise ValueError("سرویس متعلق به شما نیست")
    if not service.pg_user_id:
        raise ValueError("سرویس به پنل متصل نیست")
    if (service.remark or "").strip() == "linked":
        raise ValueError("سرویس متصل‌شده فقط مشاهده است؛ خرید افزونه ممکن نیست")
    user = await session.get(BotUser, int(user_id))
    if not user:
        raise ValueError("کاربر یافت نشد")
    from app.services.service_automation import service_shop_id
    from app.services.service_renewals import assert_service_quota_available

    if await service_shop_id(session, service) != shop_id:
        raise ValueError("سرویس این فروشگاه نیست")
    # Fail closed per kind: scoped catalog for pack.kind blocks general/category
    # packs of that kind; other kinds keep legacy rules.
    svc_cat = await service_plan_category_id(session, service)
    svc_plan = int(service.plan_id) if getattr(service, "plan_id", None) else None
    exclusive = bool(
        svc_plan
        and await plan_has_scoped_packs(
            session,
            plan_id=svc_plan,
            reseller_id=shop_id,
            kind=pack.kind,
        )
    )
    if not pack_matches_service(
        pack,
        service_plan_id=svc_plan,
        service_category_id=svc_cat,
        plan_catalog_exclusive=exclusive,
    ):
        raise ValueError(
            "این بسته برای پلن این سرویس نیست — بسته متناسب با همان سرویس را انتخاب کنید"
        )
    await assert_service_quota_available(session, service.id)
    if service.quota_synced_at is not None:
        if pack.kind == KIND_VOLUME and int(service.quota_data_limit_bytes or 0) <= 0:
            raise ValueError("این سرویس حجم نامحدود دارد — بسته حجم قابل خرید نیست")
        if pack.kind == KIND_DURATION and service.quota_expire_at is None:
            raise ValueError("این سرویس زمان نامحدود دارد — بسته زمان قابل خرید نیست")
    if pack.kind == KIND_VOLUME:
        await _assert_pack_volume_available(session, shop_id, pack)
    return addon_terms(pack)


async def preview_addon(
    session: AsyncSession,
    *,
    user_id: int,
    service: UserService,
    pack: ServiceAddonPack,
) -> dict[str, Any]:
    """Live before/after preview for Mini App confirmation — no financial writes."""
    from app.services.orders import _shop_reseller_id
    from app.services.pasarguard import get_pg, get_pg_for_reseller

    shop_id = _shop_reseller_id()
    terms = await validate_addon_selection(
        session, user_id=user_id, service=service, pack=pack, shop_id=shop_id,
    )
    pg = await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()
    result = calculate_addon_preview(
        await pg.get_user_by_id(service.pg_user_id),
        kind=pack.kind,
        amount=float(pack.amount),
    )
    quote = await quote_product(session, pack, shop_id=shop_id)
    return {
        **result,
        **quote.payload(),
        "terms": terms,
        "pack_name": pack.name,
        "kind": pack.kind,
        "amount_label": amount_label(pack),
        "request_key": secrets.token_hex(16),
    }


async def _assert_pack_volume_available(
    session: AsyncSession, shop_rid: int | None, pack: ServiceAddonPack
) -> None:
    """Refuse a volume pack the shop cannot apply within its ceiling; delivery holds it again."""
    try:
        await assert_volume_delta_allowed(
            session, shop_id=shop_rid, delta_bytes=pack_volume_bytes(pack.amount), audience="customer",
        )
    except VolumeAllocationError as e:
        raise ValueError(e.user_message()) from e


def _parse_addon_request_key(request_key: str | None) -> str | None:
    if request_key is None:
        return None
    if (
        not isinstance(request_key, str)
        or len(request_key) != 32
        or any(c not in string.hexdigits for c in request_key)
    ):
        raise ValueError("شناسه درخواست افزایش نامعتبر است")
    return request_key


async def create_addon_order(
    session: AsyncSession,
    *,
    user_id: int,
    service: UserService,
    pack: ServiceAddonPack,
    commit: bool = True,
    expected_price: int | None = None,
    request_key: str | None = None,
) -> Order:
    """Create a pending addon order. Price is snapshotted from the pack."""
    from app.services.orders import _shop_reseller_id

    shop_rid = _shop_reseller_id()
    key = _parse_addon_request_key(request_key)
    if key is not None:
        existing = await session.scalar(select(Order).where(Order.addon_request_key == key))
        if existing is not None:
            note = parse_addon_note(existing.note)
            pack_id = note[0] if note else None
            if (
                existing.user_id,
                existing.service_id,
                pack_id,
                existing.reseller_id,
            ) != (user_id, service.id, int(pack.id), shop_rid):
                raise ValueError("درخواست افزایش نامعتبر است")
            return existing

    await validate_addon_selection(
        session, user_id=user_id, service=service, pack=pack, shop_id=shop_rid,
    )
    await _assert_pack_affordable(session, shop_rid, service, pack)

    quote = await quote_product(session, pack, shop_id=shop_rid)
    assert_expected_price(quote, expected_price)
    order = Order(
        user_id=user_id,
        plan_id=None,
        amount=quote.price,
        status=OrderStatus.PENDING.value,
        note=format_addon_note(
            int(pack.id),
            int(service.id),
            kind=pack.kind,
            amount=float(pack.amount),
        ),
        service_id=int(service.id),
        reseller_id=shop_rid,
        addon_request_key=key,
    )
    if key is None:
        session.add(order)
    else:
        connection = await session.connection()
        if connection.dialect.name == "sqlite":
            raw = await connection.get_raw_connection()
            if not raw.driver_connection.in_transaction:
                await connection.exec_driver_sql("BEGIN")
        try:
            async with session.begin_nested():
                session.add(order)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(select(Order).where(Order.addon_request_key == key))
            if existing is None:
                raise
            note = parse_addon_note(existing.note)
            pack_id = note[0] if note else None
            if (
                existing.user_id,
                existing.service_id,
                pack_id,
                existing.reseller_id,
            ) != (user_id, service.id, int(pack.id), shop_rid):
                raise ValueError("درخواست افزایش نامعتبر است") from None
            return existing
    if commit:
        await session.commit()
        await session.refresh(order)
    else:
        await session.flush()
    return order


async def apply_service_addon(
    session: AsyncSession, order: Order
) -> Order:
    """Apply paid addon to the linked UserService (additive volume/time)."""
    parsed = parse_addon_note(order.note)
    if not parsed:
        raise ValueError("سفارش افزونه نامعتبر است")
    pack_id, service_id, snap_kind, snap_amount = parsed
    order_id = int(order.id)

    try:
        with session.no_autoflush:
            from app.services.service_cancellations import lock_service_mutation
            await lock_service_mutation(session, service_id)
            claim = await session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.PAID.value,
                )
                .values(status=OrderStatus.DELIVERING.value, service_mutation_pending=True)
                .execution_options(synchronize_session=False)
            )
    except IntegrityError as exc:
        await session.rollback()
        await session.refresh(order)
        await session.get(BotUser, order.user_id)
        raise ValueError("تغییر دیگری برای این سرویس در حال انجام یا بررسی است") from exc
    if claim.rowcount != 1:
        await session.refresh(order)
        if order.status == OrderStatus.DELIVERED.value:
            return order
        raise ValueError("سفارش افزونه قابل پردازش نیست")
    await session.commit()
    await session.refresh(order)

    async def _release() -> None:
        await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.DELIVERING.value,
            )
            .values(status=OrderStatus.PAID.value, service_mutation_pending=False)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    pg_applied = False
    try:
        pack = await session.get(ServiceAddonPack, pack_id)
        service = await session.get(UserService, service_id)
        if not pack or not service:
            raise ValueError("بسته یا سرویس یافت نشد")
        if int(order.service_id or 0) != int(service.id):
            raise ValueError("سرویس سفارش با یادداشت همخوان نیست")
        if int(service.bot_user_id) != int(order.user_id):
            raise ValueError("سرویس متعلق به خریدار نیست")
        # Shop isolation: pack + order must match
        if order.reseller_id:
            if int(pack.owner_reseller_id or 0) != int(order.reseller_id):
                raise ValueError("بسته متعلق به این فروشگاه نیست")
        elif pack.owner_reseller_id is not None:
            raise ValueError("بسته متعلق به این فروشگاه نیست")

        # Prefer snapshotted kind/amount from the order note; fall back to live pack
        # only for legacy notes written before snapshotting.
        apply_kind = snap_kind or pack.kind
        apply_amount = float(snap_amount) if snap_amount is not None else float(pack.amount)
        if apply_kind not in VALID_KINDS:
            raise ValueError("نوع بسته نامعتبر است")

        # PG mutate first. After it succeeds we must not release the claim —
        # releasing would let a retry double-apply capacity.
        await _apply_pack_to_service(
            session,
            service,
            kind=apply_kind,
            amount=apply_amount,
            order_reseller_id=order.reseller_id,
            order_id=int(order_id),
        )
        pg_applied = True

        with session.no_autoflush:
            done = await session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.DELIVERING.value,
                )
                .values(status=OrderStatus.DELIVERED.value, service_mutation_pending=False)
                .execution_options(synchronize_session=False)
            )
        if done.rowcount != 1:
            # PG already mutated — do not release; seal below in except path.
            log.error(
                "addon deliver seal failed after PG apply order=%s", order_id
            )
            raise ValueError("ثبت تحویل افزونه ناموفق بود")
        from app.services.loyalty import consume_loyalty_discount_for_order

        await consume_loyalty_discount_for_order(session, order)
        await session.commit()
        await session.refresh(order)
        return order
    except Exception:
        if not pg_applied:
            try:
                await _release()
            except Exception:
                log.exception("addon claim release failed order=%s", order_id)
        else:
            # Capacity already on PG — leave order DELIVERED so retries do not
            # re-apply. Prefer seal over leaving DELIVERING/PAID.
            try:
                await session.execute(
                    update(Order)
                    .where(
                        Order.id == order_id,
                        Order.status == OrderStatus.DELIVERING.value,
                    )
                    .values(status=OrderStatus.DELIVERED.value, service_mutation_pending=False)
                    .execution_options(synchronize_session=False)
                )
                from app.services.loyalty import consume_loyalty_discount_for_order

                await consume_loyalty_discount_for_order(session, order)
                await session.commit()
            except Exception:
                log.exception(
                    "addon post-PG seal failed order=%s — manual check needed",
                    order_id,
                )
        raise


async def _on_hold_activation_expire_ts(
    session: AsyncSession,
    service: UserService,
    info: dict[str, Any],
    *,
    now: int,
) -> int:
    """Resolve a finite expiration before activating a pending-start user."""
    from app.services.formatting import on_hold_expire_duration_seconds, parse_expire

    current = parse_expire(info.get("expire") or info.get("expire_date"))
    if current is not None:
        return int(current.timestamp())
    duration = on_hold_expire_duration_seconds(info)
    if duration is not None:
        return now + duration

    # apply_service_addon loads UserService via session.get, so plan may be
    # unloaded. Never trigger an async relationship load through getattr.
    plan = service.__dict__.get("plan")
    if plan is None and service.plan_id:
        plan = await session.get(Plan, int(service.plan_id))
    plan_days = int(getattr(plan, "duration_days", 0) or 0) if plan else 0
    if plan_days <= 0:
        raise ValueError("مدت سرویس در انتظار مشخص نیست — بسته قابل اعمال نیست")
    return now + plan_days * 86400


async def _hold_pack_volume(
    session: AsyncSession, service: UserService, *, shop_id: int | None, new_limit: int | None, key: str
) -> HeldVolume | None:
    """Hold a volume pack's increase in the shop's ceiling before the panel write."""
    if new_limit is None or not shop_id:
        return None
    try:
        return await hold_limit_change(
            session, shop_id=int(shop_id), current_limit=service.quota_data_limit_bytes,
            new_limit=int(new_limit), key=key, audience="customer",
        )
    except ProvisionError as e:
        raise ValueError(e.message) from e


async def _charge_pack(
    session: AsyncSession,
    service: UserService,
    *,
    shop_id: int | None,
    change: ServiceChange,
    order_id: int | None,
    hold: HeldVolume | None,
) -> ResellerCharge | None:
    """Take the shop's PAYG charge for a pack before the panel write; frees the hold on refusal."""
    if not shop_id or not await shop_charges_at_creation(session, int(shop_id)):
        return None
    try:
        return await charge_shop_change(
            session,
            shop_id=int(shop_id),
            change=change,
            request_key=f"addon:{int(order_id)}" if order_id else f"addon:{new_request_key()}",
            pg_username=str(service.pg_username or ""),
            panel_target={"service_id": int(service.id), "order_id": order_id},
            created_by="addon",
        )
    except ProvisionError as e:
        log.warning("payg pack charge refused service=%s: %s", service.id, e.message)
        await release_volume_hold(session, hold)
        await session.commit()
        raise ValueError(SHORTFALL_FOR_CUSTOMER) from e


async def _assert_pack_affordable(session: AsyncSession, shop_id: int | None, service: UserService, pack) -> None:
    """Refuse a pack order its PAYG shop cannot pay for, before the buyer pays (cached limits)."""
    if not shop_id:
        return
    current = int(service.quota_data_limit_bytes or 0)
    if pack.kind == KIND_VOLUME:
        new_limit = current + pack_volume_bytes(float(pack.amount))
        change = _pack_change(KIND_VOLUME, current_limit=current, new_limit=new_limit, days=0)
    else:
        change = _pack_change(pack.kind, current_limit=current, new_limit=None, days=int(float(pack.amount)))
    try:
        await assert_shop_can_afford(session, int(shop_id), change)
    except ProvisionError as e:
        log.warning("payg pack checkout refused shop=%s: %s", shop_id, e.message)
        raise ValueError(SHORTFALL_FOR_CUSTOMER) from e


def _pack_change(kind: str, *, current_limit: int | None, new_limit: int | None, days: int) -> ServiceChange:
    if kind == KIND_VOLUME:
        return ServiceChange(
            ChargeEvent.ADD_VOLUME, data_limit_bytes=int(new_limit or 0), previous_limit_bytes=int(current_limit or 0)
        )
    return ServiceChange(ChargeEvent.ADD_DAYS, days=days)


async def _write_pack(
    session: AsyncSession, pg: Any, service: UserService, payload: dict[str, Any], hold: HeldVolume | None
) -> None:
    """Write the pack to the panel; the hold is dropped in the caller's commit of the new limit."""
    try:
        await pg.modify_user_by_id(int(service.pg_user_id), payload)
    except Exception as exc:
        # Refused by the panel: nothing was granted. Any other failure keeps the hold until it expires.
        if is_hard_panel_rejection(exc):
            await release_volume_hold(session, hold)
        raise
    await release_volume_hold(session, hold)


async def _write_charged_pack(
    session: AsyncSession, pg: Any, service: UserService, payload: dict[str, Any],
    hold: HeldVolume | None, charge: ResellerCharge | None,
) -> None:
    """Write the pack; its charge settles in the caller's commit of the new limit, like the hold."""
    try:
        await _write_pack(session, pg, service, payload, hold)
    except Exception as exc:
        await settle_failed_charge(session, charge, exc, panel_touched=True)
        raise
    await finish_charge(session, charge, commit=False)


async def _apply_pack_to_service(
    session: AsyncSession,
    service: UserService,
    *,
    kind: str,
    amount: float,
    order_reseller_id: int | None,
    order_id: int | None = None,
) -> None:
    """Additive PG mutate — never falls back across shop PG credentials.

    Unlimited volume/time services are rejected (not converted to limited).
    """
    from app.services.bot_user_admin import sync_service_quota_cache
    from app.services.formatting import is_on_hold_status, parse_expire
    from app.services.pasarguard import get_pg, get_pg_for_reseller

    if not service.pg_user_id:
        raise ValueError("سرویس به پنل متصل نیست")

    if order_reseller_id:
        pg = await get_pg_for_reseller(session, int(order_reseller_id))
    else:
        from app.services.users import current_shop_reseller_id

        if current_shop_reseller_id():
            raise ValueError("فروشگاه بدون اعتبار پاسارگارد معتبر است")
        pg = get_pg()

    info = await pg.get_user_by_id(int(service.pg_user_id))
    if not isinstance(info, dict):
        raise ValueError("کاربر پاسارگارد یافت نشد")

    payload: dict[str, Any] = {"status": "active"}
    expire_ts = None
    data_limit_bytes = None
    current_limit: int | None = None
    pack_days = 0

    if kind == KIND_VOLUME:
        add_bytes = pack_volume_bytes(amount)
        if add_bytes <= 0:
            raise ValueError("حجم نامعتبر است")
        try:
            current = int(info.get("data_limit") or 0)
        except (TypeError, ValueError):
            current = 0
        if current <= 0:
            raise ValueError("این سرویس حجم نامحدود دارد — بسته حجم قابل اعمال نیست")
        current_limit = current
        data_limit_bytes = current + add_bytes
        payload["data_limit"] = data_limit_bytes
        if is_on_hold_status(info.get("status")):
            # Switching to active does not start PG's pending expiration timer.
            expire_ts = await _on_hold_activation_expire_ts(
                session, service, info, now=int(time.time())
            )
            payload["expire"] = expire_ts
    elif kind == KIND_DURATION:
        days = int(float(amount))
        if days <= 0:
            raise ValueError("مدت نامعتبر است")
        pack_days = days
        now = int(time.time())
        cur = parse_expire(info.get("expire") or info.get("expire_date"))
        if cur is None and is_on_hold_status(info.get("status")):
            base = await _on_hold_activation_expire_ts(
                session, service, info, now=now
            )
            expire_ts = base + days * 86400
        elif cur is None:
            raise ValueError("این سرویس زمان نامحدود دارد — بسته زمان قابل اعمال نیست")
        else:
            base = max(now, int(cur.timestamp()))
            expire_ts = base + days * 86400
        payload["expire"] = expire_ts
        if "data_limit" in info:
            # Preserve the live volume quota explicitly, and cache it alongside
            # the new expiration so a time-only write cannot look unlimited.
            try:
                data_limit_bytes = int(info.get("data_limit") or 0)
            except (TypeError, ValueError) as e:
                raise ValueError("حجم سرویس نامعتبر است") from e
            if data_limit_bytes < 0:
                raise ValueError("حجم سرویس نامعتبر است")
            payload["data_limit"] = data_limit_bytes
    else:
        raise ValueError("نوع بسته پشتیبانی نمی‌شود")

    hold = await _hold_pack_volume(
        session, service, shop_id=order_reseller_id,
        new_limit=data_limit_bytes if kind == KIND_VOLUME else None, key=f"addon:{int(order_id)}" if order_id else new_hold_key("addon"),
    )
    charge = await _charge_pack(
        session, service, shop_id=order_reseller_id, order_id=order_id, hold=hold,
        change=_pack_change(kind, current_limit=current_limit, new_limit=data_limit_bytes, days=pack_days),
    )
    await _write_charged_pack(session, pg, service, payload, hold, charge)
    # Cache sync is best-effort — PG already mutated. Never fail the delivery
    # claim here or a retry would double-apply the same addon.
    try:
        sync_service_quota_cache(
            service,
            expire_ts=expire_ts,
            data_limit_bytes=data_limit_bytes,
        )
    except Exception:
        log.exception(
            "addon quota cache sync failed svc=%s kind=%s",
            getattr(service, "id", None),
            kind,
        )
