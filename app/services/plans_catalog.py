"""Sales-plan helpers shared by admin and reseller surfaces."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FunnelEvent,
    Order,
    PgAdminSubscription,
    Plan,
    PointsRule,
    ResellerApplication,
    ResellerPlan,
    ResellerProfile,
    UserService,
)
from app.services.pasarguard import as_list, get_pg
from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
from app.services.shop_scope import ShopScopeError, is_platform_admin, shop_owner_id

log = logging.getLogger(__name__)


class PlanDeleteBlocked(Exception):
    """Plan cannot be deleted because dependents still reference it (FK RESTRICT)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


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


def _admin_pg_unrestricted(staff: dict | None) -> bool:
    """True for a genuine platform Owner whose env PasarGuard account has no
    group/template restriction.

    ``role == "admin"`` alone is not enough: a Hybrid Owner setup may point
    the platform ``.env`` PG credentials at a *limited* PasarGuard admin
    (``pg_is_owner`` is explicitly ``False`` in that case, set by
    ``pg_access.enrich_platform_admin_staff``). Such an account must go
    through the exact same ``allowed_group_ids`` / ``allowed_template_ids``
    filtering as any other role — otherwise a platform admin's own
    PasarGuard-side group restriction (e.g. only groups 1 and 2) would be
    silently ignored in the web panel. Missing key (unenriched / legacy
    staff dict, or non-Hybrid single-owner deployments) defaults to True to
    preserve prior behavior.
    """
    if not staff or staff.get("role") != "admin":
        return False
    return bool(staff.get("pg_is_owner", True))


def _allowed_id_set(raw) -> set[int] | None:
    """Parse allow-list.

    ``None`` input → None (unresolved — callers must not treat as unrestricted
    for non-Owner mutations). Invalid payloads → empty set (deny everything).
    """
    if raw is None:
        return None
    try:
        return {int(x) for x in raw}
    except (TypeError, ValueError):
        return set()


def filter_templates_for_staff(
    items: list[dict],
    staff: dict | None,
    *,
    trust_client_scope: bool | None = None,
) -> list[dict]:
    """Filter templates for restricted staff.

    When ``allowed_template_ids`` is None:
    - trust_client_scope True (reseller own-token lists) → keep items
    - trust_client_scope False → fail closed ``[]`` (no unrestricted owner lists)
    """
    if not staff or _admin_pg_unrestricted(staff):
        return items
    if trust_client_scope is None:
        from app.services.pg_read import trust_pg_list_scope

        trust_client_scope = trust_pg_list_scope(staff)
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_template_ids"))
    if allowed is None:
        return items if trust_client_scope else []
    if not allowed:
        return []
    return [t for t in items if int(t.get("id") or 0) in allowed]


def filter_groups_for_staff(
    items: list[dict],
    staff: dict | None,
    *,
    trust_client_scope: bool | None = None,
) -> list[dict]:
    if not staff or _admin_pg_unrestricted(staff):
        return items
    if trust_client_scope is None:
        from app.services.pg_read import trust_pg_list_scope

        trust_client_scope = trust_pg_list_scope(staff)
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_group_ids"))
    if allowed is None:
        return items if trust_client_scope else []
    if not allowed:
        return []
    return [g for g in items if int(g.get("id") or 0) in allowed]


def template_allowed_for_staff(staff: dict | None, template_id: int | None) -> bool:
    """Mutation/object check. Missing allow-list is NOT unrestricted for non-Owner (M1)."""
    if template_id is None:
        return False
    if not staff or _admin_pg_unrestricted(staff):
        return True
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_template_ids"))
    if allowed is None:
        # Unresolved allow-list ≠ unrestricted. Hybrid limited admin → deny.
        # Reseller / credentialed pg_staff → own-client scopes the ID space.
        from app.services.pg_read import staff_pg_credentials_ready
        from app.services.shop_scope import is_platform_admin

        if is_platform_admin(staff):
            return False
        return bool(staff_pg_credentials_ready(staff))
    if not allowed:
        return False
    return int(template_id) in allowed


def groups_allowed_for_staff(staff: dict | None, group_ids: list[int]) -> bool:
    """Mutation/object check. Missing allow-list is NOT unrestricted for non-Owner (M1)."""
    if not group_ids:
        return False
    if not staff or _admin_pg_unrestricted(staff):
        return True
    access = staff.get("pg_access") or {}
    allowed = _allowed_id_set(access.get("allowed_group_ids"))
    if allowed is None:
        from app.services.pg_read import staff_pg_credentials_ready
        from app.services.shop_scope import is_platform_admin

        if is_platform_admin(staff):
            return False
        return bool(staff_pg_credentials_ready(staff))
    if not allowed:
        return False
    return all(int(g) in allowed for g in group_ids)


def staff_can_create_pg_template(staff: dict | None) -> bool:
    """True only when the principal may perform templates.create (exact ACL)."""
    if not staff:
        return False
    from app.services.pg_access import staff_pg_action

    # Hybrid: platform admin also needs templates.create on the env PG role.
    return staff_pg_action(staff, "templates", "create")


async def load_pg_plan_options(
    staff: dict | None = None,
    *,
    session=None,
) -> tuple[list[dict], list[dict], str | None]:
    """Templates + groups for plan forms, filtered to staff PG access.

    Uses tenant-safe read client (C1). Restricted staff without own credentials
    get empty lists (no owner-token fetch).
    """
    templates: list[dict] = []
    groups: list[dict] = []
    pg_error = None
    try:
        from app.services.pg_read import PgReadDenied, staff_pg_read_client, trust_pg_list_scope

        if staff and staff.get("role") != "admin":
            try:
                pg = await staff_pg_read_client(session, staff)
            except PgReadDenied as e:
                return [], [], e.message
        else:
            pg = get_pg()
        try:
            await pg.ensure_token()
        except Exception as e:
            log.warning("load_pg_plan_options token failed: %s", e)
            return [], [], "اتصال به پاسارگارد برقرار نشد"
        try:
            templates = await pg.get_user_templates_simple()
            full = await pg.get_user_templates()
            if isinstance(full, list) and full:
                templates = full
            else:
                templates = as_list(full, "templates") or templates
        except Exception as e:
            log.warning("load_pg_plan_options templates unavailable: %s", e)
            templates = []
        try:
            groups = await pg.get_groups_simple()
        except Exception as e:
            log.warning("load_pg_plan_options groups unavailable: %s", e)
            groups = []
    except Exception as e:
        # Never surface raw PG/HTTP exception text to the panel (info leak).
        log.warning("load_pg_plan_options failed: %s", e)
        pg_error = "اتصال به پاسارگارد برقرار نشد"
    templates = [t for t in templates if isinstance(t, dict)]
    groups = [g for g in groups if isinstance(g, dict)]
    trust = trust_pg_list_scope(staff) if staff else True
    templates = filter_templates_for_staff(templates, staff, trust_client_scope=trust)
    groups = filter_groups_for_staff(groups, staff, trust_client_scope=trust)
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


async def plan_limit_issue(staff: dict | None, plan: Plan | None, *, session=None) -> str | None:
    """Return a human message when a saved user-facing plan exceeds live PG limits."""
    if not plan or not staff:
        return None
    try:
        data_limit = (
            int(float(plan.data_limit_gb) * (1024**3))
            if plan.data_limit_gb is not None
            else None
        )
    except (TypeError, ValueError):
        return "حجم پلن نامعتبر است"
    try:
        await assert_user_plan_within_limits(
            staff,
            data_limit=data_limit,
            duration_days=int(plan.duration_days or 0),
            label="پلن",
            session=session,
        )
    except PgQuotaError as exc:
        return exc.message
    if plan.pg_template_id and not template_allowed_for_staff(staff, int(plan.pg_template_id)):
        return "تمپلیت انتخاب‌شده دیگر در دسترس این حساب نیست"
    if plan.pg_group_ids:
        try:
            gids = [
                int(x.strip()) for x in str(plan.pg_group_ids).split(",") if x.strip()
            ]
        except (TypeError, ValueError):
            return "گروه‌های پلن نامعتبر است"
        if gids and not groups_allowed_for_staff(staff, gids):
            return "یکی از گروه‌های این پلن دیگر در دسترس این حساب نیست"
    return None


def custom_range_limit_issue(
    *,
    min_gb: float,
    max_gb: float,
    min_days: int,
    max_days: int,
    snapshot: dict[str, Any] | None,
) -> str | None:
    """Pure UI hint from a preloaded quota snapshot for custom-plan settings."""
    snap = snapshot or {}
    if not snap.get("restricted"):
        return None
    data_max = snap.get("per_user_data_max")
    if data_max is not None and data_max > 0:
        try:
            if int(float(max_gb) * (1024**3)) > int(data_max):
                from app.services.formatting import format_bytes

                return f"حداکثر حجم پلن دلخواه نمی‌تواند بیشتر از {format_bytes(data_max)} باشد"
        except (TypeError, ValueError):
            return "حداکثر حجم پلن دلخواه نامعتبر است"
    expire_max = snap.get("per_user_expire_max")
    if expire_max is not None and expire_max > 0:
        now = datetime.now(timezone.utc).timestamp()
        seconds = int(max_days or 0) * 86400
        if seconds <= 0:
            return "حداکثر مدت پلن دلخواه باید مشخص باشد"
        if now + seconds - now > int(expire_max):
            days = max(1, int(int(expire_max) / 86400))
            return f"حداکثر مدت پلن دلخواه نمی‌تواند بیشتر از {days} روز باشد"
    return None


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


async def _count_where(session: AsyncSession, model, column, plan_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(model).where(column == int(plan_id))
        )
        or 0
    )


async def shop_plan_reference_counts(
    session: AsyncSession, plan_id: int
) -> dict[str, int]:
    """How many dependent rows still point at this sales plan (FK RESTRICT)."""
    pid = int(plan_id)
    return {
        "services": await _count_where(session, UserService, UserService.plan_id, pid),
        "orders": await _count_where(session, Order, Order.plan_id, pid),
        "points_rules": await _count_where(session, PointsRule, PointsRule.plan_id, pid),
        "funnel_events": await _count_where(
            session, FunnelEvent, FunnelEvent.plan_id, pid
        ),
    }


def format_shop_plan_delete_blocked(
    refs: dict[str, int], *, plan_name: str | None = None
) -> str:
    """Clear Persian warning when a sales plan cannot be deleted."""
    parts: list[str] = []
    n_svc = int(refs.get("services") or 0)
    n_ord = int(refs.get("orders") or 0)
    n_pts = int(refs.get("points_rules") or 0)
    n_fun = int(refs.get("funnel_events") or 0)
    if n_svc:
        parts.append(f"{n_svc} سرویس کاربر")
    if n_ord:
        parts.append(f"{n_ord} سفارش")
    if n_pts:
        parts.append(f"{n_pts} قانون امتیاز")
    if n_fun:
        parts.append(f"{n_fun} رویداد قیف فروش")
    label = f"پلن «{plan_name}»" if plan_name else "این پلن"
    if not parts:
        return (
            f"{label} قابل حذف نیست چون هنوز به دادهٔ وابسته وصل است. "
            "پلن‌هایی که کاربر/سفارش دارند را نمی‌توان حذف کرد."
        )
    joined = "، ".join(parts)
    return (
        f"{label} قابل حذف نیست چون هنوز به دادهٔ وابسته وصل است ({joined}). "
        "پلن‌هایی که کاربر دارند حذف نمی‌شوند — ابتدا سرویس‌ها و سفارش‌های "
        "مرتبط را جابه‌جا یا پاک کنید، بعد دوباره تلاش کنید."
    )


async def delete_shop_plan(session: AsyncSession, plan: Plan) -> None:
    """Delete a sales plan after verifying no FK dependents remain.

    Raises PlanDeleteBlocked with a clear Persian message instead of letting
    SQLAlchemy raise IntegrityError / PendingRollbackError (HTTP 500).
    Does not commit — caller owns the transaction.
    """
    if getattr(plan, "is_trial", False):
        raise PlanDeleteBlocked("پلن آزمایشی از این مسیر حذف نمی‌شود")
    refs = await shop_plan_reference_counts(session, int(plan.id))
    if any(int(v or 0) > 0 for v in refs.values()):
        raise PlanDeleteBlocked(
            format_shop_plan_delete_blocked(refs, plan_name=getattr(plan, "name", None))
        )
    await session.delete(plan)
    try:
        await session.flush()
    except IntegrityError as exc:
        log.warning(
            "shop plan delete blocked by FK after pre-check plan_id=%s: %s",
            getattr(plan, "id", None),
            exc,
        )
        raise PlanDeleteBlocked(
            format_shop_plan_delete_blocked(
                refs, plan_name=getattr(plan, "name", None)
            )
            if any(int(v or 0) > 0 for v in refs.values())
            else (
                "این پلن قابل حذف نیست چون هنوز به دادهٔ وابسته "
                "(سرویس کاربر، سفارش و …) وصل است. "
                "پلن‌هایی که کاربر دارند را نمی‌توان حذف کرد."
            )
        ) from None


async def reseller_plan_reference_counts(
    session: AsyncSession, plan_id: int
) -> dict[str, int]:
    """How many dependent rows still point at this reseller package plan."""
    pid = int(plan_id)
    return {
        "profiles": await _count_where(
            session, ResellerProfile, ResellerProfile.plan_id, pid
        ),
        "subscriptions": await _count_where(
            session, PgAdminSubscription, PgAdminSubscription.plan_id, pid
        ),
        "applications": await _count_where(
            session, ResellerApplication, ResellerApplication.plan_id, pid
        ),
    }


def format_reseller_plan_delete_blocked(
    refs: dict[str, int], *, plan_name: str | None = None
) -> str:
    parts: list[str] = []
    n_prof = int(refs.get("profiles") or 0)
    n_sub = int(refs.get("subscriptions") or 0)
    n_app = int(refs.get("applications") or 0)
    if n_prof:
        parts.append(f"{n_prof} نماینده")
    if n_sub:
        parts.append(f"{n_sub} اشتراک پاسارگارد")
    if n_app:
        parts.append(f"{n_app} درخواست نمایندگی")
    label = f"پلن نمایندگی «{plan_name}»" if plan_name else "این پلن نمایندگی"
    if not parts:
        return (
            f"{label} قابل حذف نیست چون هنوز به دادهٔ وابسته وصل است. "
            "پلن‌هایی که نماینده دارند را نمی‌توان حذف کرد."
        )
    joined = "، ".join(parts)
    return (
        f"{label} قابل حذف نیست چون هنوز به دادهٔ وابسته وصل است ({joined}). "
        "ابتدا نمایندگان/اشتراک‌های مرتبط را به پلن دیگری منتقل کنید، "
        "بعد دوباره تلاش کنید."
    )


async def delete_reseller_plan(session: AsyncSession, plan: ResellerPlan) -> None:
    """Delete a reseller package plan after FK pre-check + billing-rate cleanup.

    Raises PlanDeleteBlocked. Does not commit — caller owns the transaction.
    """
    refs = await reseller_plan_reference_counts(session, int(plan.id))
    if any(int(v or 0) > 0 for v in refs.values()):
        raise PlanDeleteBlocked(
            format_reseller_plan_delete_blocked(
                refs, plan_name=getattr(plan, "name", None)
            )
        )
    from app.services.billing import delete_plan_billing_rate

    await delete_plan_billing_rate(session, int(plan.id))
    await session.delete(plan)
    try:
        await session.flush()
    except IntegrityError as exc:
        log.warning(
            "reseller plan delete blocked by FK after pre-check plan_id=%s: %s",
            getattr(plan, "id", None),
            exc,
        )
        raise PlanDeleteBlocked(
            format_reseller_plan_delete_blocked(
                refs, plan_name=getattr(plan, "name", None)
            )
            if any(int(v or 0) > 0 for v in refs.values())
            else (
                "این پلن نمایندگی قابل حذف نیست چون هنوز به دادهٔ وابسته وصل است. "
                "پلن‌هایی که نماینده دارند را نمی‌توان حذف کرد."
            )
        ) from None
