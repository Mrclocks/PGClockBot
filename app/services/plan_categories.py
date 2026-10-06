"""Shop-scoped plan categories — fail-closed like the sales-plan catalog.

Categories are additive labels (not replacements for plan kinds like
fixed/trial/custom). Each category targets an ``audience``:
``users`` (sales ``Plan``) or ``resellers`` (``ResellerPlan`` packages).
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, PlanCategory, ResellerPlan
from app.services.plans_catalog import (
    apply_catalog_owner_filter,
    catalog_owner_id,
    require_catalog_owner_id,
)
from app.services.shop_scope import ShopScopeError, is_platform_admin, shop_owner_id

AUDIENCE_USERS = "users"
AUDIENCE_RESELLERS = "resellers"
VALID_AUDIENCES = frozenset({AUDIENCE_USERS, AUDIENCE_RESELLERS})


def normalize_audience(raw: str | None, *, default: str = AUDIENCE_USERS) -> str:
    key = str(raw or "").strip().lower()
    if key in VALID_AUDIENCES:
        return key
    return default


def apply_category_owner_filter(query, staff: dict | None):
    """Filter categories to the staff shop. Non-admin without scope → empty."""
    if is_platform_admin(staff):
        return query.where(PlanCategory.owner_reseller_id.is_(None))
    if not staff:
        return query.where(PlanCategory.owner_reseller_id.is_(None))
    rid = shop_owner_id(staff)
    if not rid:
        return query.where(PlanCategory.id < 0)
    return query.where(PlanCategory.owner_reseller_id == rid)


def category_belongs_to_staff(cat: PlanCategory | None, staff: dict | None) -> bool:
    if not cat:
        return False
    if is_platform_admin(staff):
        return cat.owner_reseller_id is None
    if not staff:
        return cat.owner_reseller_id is None
    rid = shop_owner_id(staff)
    if not rid:
        return False
    return int(cat.owner_reseller_id or 0) == int(rid)


def category_matches_shop(cat: PlanCategory | None, shop_rid: int | None) -> bool:
    """Bot/runtime check: category must belong to the current shop context."""
    if not cat or not cat.is_active:
        return False
    if shop_rid:
        return int(cat.owner_reseller_id or 0) == int(shop_rid)
    return cat.owner_reseller_id is None


async def list_categories(
    session: AsyncSession,
    staff: dict | None,
    *,
    active_only: bool = False,
    audience: str | None = None,
) -> list[PlanCategory]:
    q = select(PlanCategory).order_by(PlanCategory.sort_order, PlanCategory.id)
    q = apply_category_owner_filter(q, staff)
    if active_only:
        q = q.where(PlanCategory.is_active.is_(True))
    if audience is not None:
        aud = normalize_audience(audience)
        q = q.where(PlanCategory.audience == aud)
    return list((await session.execute(q)).scalars().all())


async def list_shop_categories(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
    active_only: bool = True,
    audience: str | None = AUDIENCE_USERS,
) -> list[PlanCategory]:
    """Bot catalog listing by shop context (not staff)."""
    from app.services.users import current_shop_reseller_id

    rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    q = select(PlanCategory).order_by(PlanCategory.sort_order, PlanCategory.id)
    if rid:
        q = q.where(PlanCategory.owner_reseller_id == int(rid))
    else:
        q = q.where(PlanCategory.owner_reseller_id.is_(None))
    if active_only:
        q = q.where(PlanCategory.is_active.is_(True))
    if audience is not None:
        q = q.where(PlanCategory.audience == normalize_audience(audience))
    return list((await session.execute(q)).scalars().all())


async def shop_categories_for_menu(
    session: AsyncSession,
    *,
    fixed_plans: list[Plan],
    reseller_id: int | None = None,
) -> list[PlanCategory]:
    """Active user-audience categories that have ≥1 active fixed plan (shop menu).

    Empty list ⇒ legacy shop shows «ثابت» instead of category buttons.
    """
    cats = await list_shop_categories(
        session,
        reseller_id=reseller_id,
        active_only=True,
        audience=AUDIENCE_USERS,
    )
    if not cats:
        return []
    used_ids = {
        int(p.category_id)
        for p in fixed_plans
        if getattr(p, "category_id", None) is not None
    }
    # Show categories that own at least one listed fixed plan.
    # If shop has categories but none linked yet, still return [] so legacy
    # «ثابت» remains (avoids empty category taps).
    return [c for c in cats if int(c.id) in used_ids]


def uncategorized_fixed_plans(fixed_plans: list[Plan]) -> list[Plan]:
    return [p for p in fixed_plans if getattr(p, "category_id", None) is None]


def plans_in_category(fixed_plans: list[Plan], category_id: int | None) -> list[Plan]:
    if category_id is None:
        return uncategorized_fixed_plans(fixed_plans)
    cid = int(category_id)
    return [
        p
        for p in fixed_plans
        if getattr(p, "category_id", None) is not None and int(p.category_id) == cid
    ]


async def get_owned_category(
    session: AsyncSession, category_id: int, staff: dict | None
) -> PlanCategory | None:
    cat = await session.get(PlanCategory, int(category_id))
    if not category_belongs_to_staff(cat, staff):
        return None
    return cat


async def create_category(
    session: AsyncSession,
    staff: dict | None,
    *,
    name: str,
    description: str | None = None,
    sort_order: int = 0,
    audience: str = AUDIENCE_USERS,
    button_style: str | None = None,
) -> PlanCategory:
    owner_id = require_catalog_owner_id(staff)
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("نام دسته‌بندی الزامی است")
    if len(cleaned) > 128:
        raise ValueError("نام دسته‌بندی خیلی طولانی است")
    desc = (description or "").strip() or None
    if desc and len(desc) > 255:
        raise ValueError("توضیح خیلی طولانی است")
    aud = normalize_audience(audience)
    # Only platform Owner may create reseller-audience labels (ResellerPlan packages).
    if aud == AUDIENCE_RESELLERS and not is_platform_admin(staff):
        raise ShopScopeError("برچسب نمایندگان فقط برای ادمین اصلی است")
    from app.services.button_styles import normalize_style

    # None = inherit; "" = white; primary/success/danger explicit
    style_val: str | None
    if button_style is None:
        style_val = None
    else:
        raw = str(button_style).strip().lower()
        if raw in {"inherit", "__inherit__"}:
            style_val = None
        else:
            style_val = normalize_style(raw)  # may be ""
    cat = PlanCategory(
        name=cleaned,
        description=desc,
        audience=aud,
        owner_reseller_id=owner_id,
        sort_order=int(sort_order or 0),
        button_style=style_val,
        is_active=True,
    )
    session.add(cat)
    await session.commit()
    await session.refresh(cat)
    return cat


async def update_category(
    session: AsyncSession,
    staff: dict | None,
    category_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
    audience: str | None = None,
    button_style: str | None | object = ...,
) -> PlanCategory:
    cat = await get_owned_category(session, category_id, staff)
    if not cat:
        raise ShopScopeError("دسته‌بندی یافت نشد")
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("نام دسته‌بندی الزامی است")
        if len(cleaned) > 128:
            raise ValueError("نام دسته‌بندی خیلی طولانی است")
        cat.name = cleaned
    if description is not None:
        desc = description.strip() or None
        if desc and len(desc) > 255:
            raise ValueError("توضیح خیلی طولانی است")
        cat.description = desc
    if sort_order is not None:
        cat.sort_order = int(sort_order)
    if is_active is not None:
        cat.is_active = bool(is_active)
    if audience is not None:
        aud = normalize_audience(audience)
        if aud == AUDIENCE_RESELLERS and not is_platform_admin(staff):
            raise ShopScopeError("برچسب نمایندگان فقط برای ادمین اصلی است")
        # Refuse audience flip when plans of the other type still reference it.
        if aud != normalize_audience(getattr(cat, "audience", None)):
            linked = await _linked_plan_names(session, cat)
            if linked:
                raise ValueError(
                    "اول پلن‌های این برچسب را جدا کنید، بعد مخاطب را عوض کنید"
                )
        cat.audience = aud
    if button_style is not ...:
        from app.services.button_styles import normalize_style

        if button_style is None:
            cat.button_style = None
        else:
            raw = str(button_style).strip().lower()
            if raw in {"inherit", "__inherit__"}:
                cat.button_style = None
            else:
                cat.button_style = normalize_style(raw)
    await session.commit()
    await session.refresh(cat)
    return cat


async def delete_category(
    session: AsyncSession, staff: dict | None, category_id: int
) -> None:
    cat = await get_owned_category(session, category_id, staff)
    if not cat:
        raise ShopScopeError("دسته‌بندی یافت نشد")
    aud = normalize_audience(getattr(cat, "audience", None))
    if aud == AUDIENCE_RESELLERS:
        detach = (
            update(ResellerPlan)
            .where(ResellerPlan.category_id == int(category_id))
            .values(category_id=None)
            .execution_options(synchronize_session=False)
        )
        await session.execute(detach)
    else:
        # Detach plans in this shop only (defense-in-depth on owner_reseller_id).
        detach = (
            update(Plan)
            .where(Plan.category_id == int(category_id))
            .values(category_id=None)
            .execution_options(synchronize_session=False)
        )
        if cat.owner_reseller_id is None:
            detach = detach.where(Plan.owner_reseller_id.is_(None))
        else:
            detach = detach.where(Plan.owner_reseller_id == int(cat.owner_reseller_id))
        await session.execute(detach)
    await session.delete(cat)
    await session.commit()


async def resolve_category_for_plan_write(
    session: AsyncSession,
    staff: dict | None,
    category_id_raw: str | int | None,
    *,
    allow_inactive_id: int | None = None,
    expected_audience: str = AUDIENCE_USERS,
) -> int | None:
    """Parse optional category id; must belong to the same shop + audience.

    ``allow_inactive_id`` lets plan edit keep the currently assigned inactive
    category without forcing a clear/reassign.
    """
    if category_id_raw is None:
        return None
    raw = str(category_id_raw).strip()
    if not raw:
        return None
    try:
        cid = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("دسته‌بندی نامعتبر است") from e
    cat = await get_owned_category(session, cid, staff)
    if not cat:
        raise ShopScopeError("دسته‌بندی در این فروشگاه یافت نشد")
    want = normalize_audience(expected_audience)
    got = normalize_audience(getattr(cat, "audience", None))
    if got != want:
        raise ValueError("این برچسب برای مخاطب انتخاب‌شده نیست")
    if not cat.is_active:
        if allow_inactive_id is not None and int(cat.id) == int(allow_inactive_id):
            return int(cat.id)
        raise ValueError("این دسته‌بندی غیرفعال است")
    return int(cat.id)


async def category_map_for_plans(
    session: AsyncSession, staff: dict | None, plans: list
) -> dict[int, PlanCategory]:
    ids = {int(p.category_id) for p in plans if getattr(p, "category_id", None)}
    if not ids:
        return {}
    q = select(PlanCategory).where(PlanCategory.id.in_(ids))
    q = apply_category_owner_filter(q, staff)
    rows = list((await session.execute(q)).scalars().all())
    return {int(c.id): c for c in rows}


async def _linked_plan_names(
    session: AsyncSession, cat: PlanCategory, *, limit: int = 12
) -> list[str]:
    aud = normalize_audience(getattr(cat, "audience", None))
    names: list[str] = []
    if aud == AUDIENCE_RESELLERS:
        q = (
            select(ResellerPlan.name)
            .where(ResellerPlan.category_id == int(cat.id))
            .order_by(ResellerPlan.sort_order, ResellerPlan.id)
            .limit(limit)
        )
        names = [str(n) for n in (await session.execute(q)).scalars().all()]
    else:
        q = select(Plan.name).where(Plan.category_id == int(cat.id))
        if cat.owner_reseller_id is None:
            q = q.where(Plan.owner_reseller_id.is_(None))
        else:
            q = q.where(Plan.owner_reseller_id == int(cat.owner_reseller_id))
        q = q.order_by(Plan.sort_order, Plan.id).limit(limit)
        names = [str(n) for n in (await session.execute(q)).scalars().all()]
    return names


async def category_plans_map(
    session: AsyncSession,
    staff: dict | None,
    categories: list[PlanCategory],
    *,
    limit_per: int = 12,
) -> dict[int, list[str]]:
    """Map category id → plan names currently using that label (for modal UI)."""
    out: dict[int, list[str]] = {}
    for cat in categories:
        if not category_belongs_to_staff(cat, staff):
            continue
        out[int(cat.id)] = await _linked_plan_names(
            session, cat, limit=limit_per
        )
    return out


def staff_shop_key(staff: dict | None) -> str:
    """Stable key for tests / logging."""
    rid = catalog_owner_id(staff)
    return "platform" if rid is None else f"shop:{rid}"


def audience_label_fa(audience: str | None) -> str:
    aud = normalize_audience(audience)
    return "نمایندگان" if aud == AUDIENCE_RESELLERS else "کاربران"
