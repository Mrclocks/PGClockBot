"""Sales-plan helpers shared by admin and reseller surfaces."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan
from app.services.pasarguard import as_list
from app.services.shop_scope import ShopScopeError, is_platform_admin, shop_owner_id


def catalog_owner_id(staff: dict | None) -> int | None:
    """Reseller bot_user_id for their catalog; None = platform (admin) catalog only.

    Non-admin without a shop id returns None; callers must use
    ``apply_catalog_owner_filter`` / ``require_catalog_owner_id`` which fail
    closed for that case (never treat as platform).
    """
    if not staff:
        return None
    if is_platform_admin(staff):
        return None
    return shop_owner_id(staff)


def require_catalog_owner_id(staff: dict | None) -> int | None:
    """Owner id for catalog writes: admin → None (platform); reseller → positive id.

    Raises ShopScopeError for any non-admin without a shop scope so they cannot
    create/edit platform plans or settings.
    """
    if is_platform_admin(staff):
        return None
    rid = shop_owner_id(staff)
    if not rid:
        raise ShopScopeError(
            "حساب شما به فروشگاه متصل نیست — دسترسی به کاتالوگ ادمین اصلی مجاز نیست"
        )
    return rid


def apply_catalog_owner_filter(query, staff: dict | None):
    """Filter plans to the staff shop. Non-admin without scope → empty result."""
    if is_platform_admin(staff):
        return query.where(Plan.owner_reseller_id.is_(None))
    if not staff:
        # Unauthenticated/internal callers: platform catalog only
        return query.where(Plan.owner_reseller_id.is_(None))
    rid = shop_owner_id(staff)
    if not rid:
        # Fail closed: never show platform catalog to secondary staff
        return query.where(Plan.id < 0)
    return query.where(Plan.owner_reseller_id == rid)


def plan_belongs_to_staff(plan: Plan | None, staff: dict | None) -> bool:
    if not plan:
        return False
    if is_platform_admin(staff):
        return plan.owner_reseller_id is None
    if not staff:
        return plan.owner_reseller_id is None
    rid = shop_owner_id(staff)
    if not rid:
        return False
    return int(plan.owner_reseller_id or 0) == rid


def _allowed_id_set(raw) -> set[int] | None:
    """None = no restriction; empty set = nothing allowed."""
    if raw is None:
        return None
    try:
        return {int(x) for x in raw}
    except (TypeError, ValueError):
        return None


def filter_templates_for_staff(items: list[dict], staff: dict | None) -> list[dict]:
    if not staff or staff.get("role") == "admin":
        return items
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_template_ids"))
    if allowed is None:
        return items
    if not allowed:
        return []
    return [t for t in items if int(t.get("id") or 0) in allowed]


def filter_groups_for_staff(items: list[dict], staff: dict | None) -> list[dict]:
    if not staff or staff.get("role") == "admin":
        return items
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_group_ids"))
    if allowed is None:
        return items
    if not allowed:
        return []
    return [g for g in items if int(g.get("id") or 0) in allowed]


def template_allowed_for_staff(staff: dict | None, template_id: int | None) -> bool:
    if template_id is None:
        return False
    if not staff or staff.get("role") == "admin":
        return True
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_template_ids"))
    if allowed is None:
        return True
    return int(template_id) in allowed


def groups_allowed_for_staff(staff: dict | None, group_ids: list[int]) -> bool:
    if not group_ids:
        return False
    if not staff or staff.get("role") == "admin":
        return True
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_group_ids"))
    if allowed is None:
        return True
    return all(int(g) in allowed for g in group_ids)


def staff_can_create_pg_template(staff: dict | None) -> bool:
    if not staff or staff.get("role") == "admin":
        return True
    writes = staff.get("pg_writes") or {}
    return bool(writes.get("templates"))


async def load_pg_plan_options(
    staff: dict | None = None,
    session=None,
) -> tuple[list[dict], list[dict], str | None]:
    """Templates + groups for plan forms, filtered to staff PG access.

    Uses the staff-scoped PG client when ``session`` is provided for limited
    actors — never dumps Owner inventory then hopes client-side filters hold.
    Platform admin (or missing staff) may use Owner.
    """
    from app.services.authz import is_limited_pg_actor, resolve_pg_client
    from app.services.pasarguard import PasarGuardError, get_pg

    templates: list[dict] = []
    groups: list[dict] = []
    pg_error = None
    try:
        if is_limited_pg_actor(staff):
            if session is None:
                return [], [], "جلسه پایگاه‌داده برای بارگذاری تمپلیت/گروه لازم است"
            try:
                pg, _ = await resolve_pg_client(session, staff)
            except PasarGuardError as e:
                return [], [], e.user_message(fallback="رمز پاسارگارد ذخیره نشده")
        else:
            pg = get_pg()
        templates = await pg.get_user_templates_simple()
        full = await pg.get_user_templates()
        if isinstance(full, list) and full:
            templates = full
        else:
            templates = as_list(full, "templates") or templates
        groups = await pg.get_groups_simple()
    except Exception as e:
        pg_error = str(e)
    templates = [t for t in templates if isinstance(t, dict)]
    groups = [g for g in groups if isinstance(g, dict)]
    templates = filter_templates_for_staff(templates, staff)
    groups = filter_groups_for_staff(groups, staff)
    return templates, groups, pg_error


async def list_catalog_plans(
    session: AsyncSession,
    staff: dict | None,
    *,
    include_trial: bool = True,
) -> list[Plan]:
    q = apply_catalog_owner_filter(select(Plan), staff).order_by(Plan.sort_order, Plan.id)
    plans = list((await session.execute(q)).scalars().all())
    if not include_trial:
        plans = [p for p in plans if not p.is_trial]
    return plans


async def get_owned_plan(
    session: AsyncSession,
    plan_id: int,
    staff: dict | None,
) -> Plan | None:
    plan = await session.get(Plan, plan_id)
    if not plan_belongs_to_staff(plan, staff):
        return None
    return plan


def parse_group_ids_from_form(form: Any, *, prefix: str = "group_") -> list[int]:
    ids: list[int] = []
    for k, v in form.items():
        if str(k).startswith(prefix) and str(v).isdigit():
            ids.append(int(v))
    return ids
