"""Periodic subordinate (child reseller) digest for Owner and Level-1 parents.

Security:
- Children are resolved only from ``org_principals`` parent/depth edges.
- Metrics always use the child's ``ResellerProfile.user_id`` as ``reseller_id``.
- Level-2 actors have no children → empty report / skip send.
- Never trusts client-supplied parent/reseller ids.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.demo_users import non_demo_customer

from app.db.models import BotUser, OrgPrincipal, ResellerProfile, UserService
from app.services.formatting import format_toman
from app.services.service_scope import shop_services_clause
from app.services.org_principals import (
    DEPTH_ONE,
    DEPTH_TWO,
    STATUS_ACTIVE,
    ensure_owner_principal,
    is_owner_principal,
)

log = logging.getLogger(__name__)

SETTING_ENABLED = "subordinate_report_enabled"
SETTING_LAST = "subordinate_report_last"


@dataclass(frozen=True)
class SubordinateShop:
    principal_id: int
    reseller_profile_id: int
    reseller_user_id: int
    display_name: str
    is_active: bool


@dataclass(frozen=True)
class SubordinateStats:
    shop: SubordinateShop
    users_new: int
    users_total: int
    orders_new: int
    orders_delivered: int
    revenue_today: int
    services_total: int
    traffic_text: str


async def list_direct_subordinate_shops(
    session: AsyncSession,
    *,
    parent: OrgPrincipal,
) -> list[SubordinateShop]:
    """Direct shop children only: Owner→L1, L1→L2. L2→[]."""
    if parent is None:
        return []
    if is_owner_principal(parent):
        child_depth = DEPTH_ONE
    elif int(parent.depth) == DEPTH_ONE and str(parent.status) == STATUS_ACTIVE:
        child_depth = DEPTH_TWO
    else:
        return []

    result = await session.execute(
        select(OrgPrincipal, ResellerProfile, BotUser)
        .join(
            ResellerProfile,
            ResellerProfile.id == OrgPrincipal.reseller_profile_id,
        )
        .join(BotUser, BotUser.id == ResellerProfile.user_id)
        .where(
            OrgPrincipal.parent_id == int(parent.id),
            OrgPrincipal.depth == int(child_depth),
            OrgPrincipal.reseller_profile_id.is_not(None),
        )
        .order_by(OrgPrincipal.id.asc())
    )
    out: list[SubordinateShop] = []
    for principal, profile, user in result.all():
        name = (
            (user.full_name or "").strip()
            or (user.username or "").strip()
            or (profile.bot_username or "").strip()
            or (profile.web_username or "").strip()
            or f"#{int(user.id)}"
        )
        out.append(
            SubordinateShop(
                principal_id=int(principal.id),
                reseller_profile_id=int(profile.id),
                reseller_user_id=int(profile.user_id),
                display_name=name,
                is_active=bool(profile.is_active)
                and str(principal.status) == STATUS_ACTIVE,
            )
        )
    return out


async def collect_subordinate_stats(
    session: AsyncSession,
    shops: list[SubordinateShop],
    *,
    traffic_by_user_id: dict[int, str] | None = None,
) -> list[SubordinateStats]:
    """Per-child shop stats (DB-scoped). Traffic text is optional/best-effort."""
    from app.services.finance_reports import shop_ops_snapshot
    from app.services.home_overview import shop_period_stats

    traffic_by_user_id = traffic_by_user_id or {}
    out: list[SubordinateStats] = []
    for shop in shops:
        rid = int(shop.reseller_user_id)
        periods = await shop_period_stats(session, reseller_id=rid)
        day = periods.get("day") or {}
        ops = await shop_ops_snapshot(session, reseller_id=rid)
        svc_total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(UserService)
                                .where(shop_services_clause(rid), non_demo_customer(UserService.bot_user_id))
                )
            ).scalar()
            or 0
        )
        out.append(
            SubordinateStats(
                shop=shop,
                users_new=int(day.get("new_users") or 0),
                users_total=int(ops.get("total_users") or 0),
                orders_new=int(day.get("orders") or 0),
                orders_delivered=int(day.get("delivered") or 0),
                revenue_today=int(day.get("revenue") or 0),
                services_total=svc_total,
                traffic_text=(traffic_by_user_id.get(rid) or "—"),
            )
        )
    return out


def format_subordinate_report(
    rows: list[SubordinateStats],
    *,
    title: str = "گزارش نمایندگان",
    period_label: str = "امروز",
) -> str:
    """Telegram HTML digest — one block per direct child."""
    lines = [
        f"📋 <b>{html.escape(title)}</b>",
        f"📅 دوره: {html.escape(period_label)}",
        "",
    ]
    if not rows:
        lines.append("زیرمجموعه‌ای برای گزارش نیست.")
        return "\n".join(lines)

    for i, row in enumerate(rows, start=1):
        name = html.escape(row.shop.display_name)
        status = "" if row.shop.is_active else " <i>(غیرفعال)</i>"
        lines.append(f"<b>{i}) {name}</b>{status}")
        lines.append(f"• کاربران جدید: {row.users_new}")
        lines.append(f"• کل کاربران: {row.users_total}")
        lines.append(f"• سفارش / خرید: {row.orders_new}")
        lines.append(f"• فروش (تحویل): {row.orders_delivered}")
        lines.append(f"• مبلغ فروش: {html.escape(format_toman(row.revenue_today))}")
        lines.append(f"• سرویس‌ها: {row.services_total}")
        lines.append(f"• حجم: <code>{html.escape(row.traffic_text)}</code>")
        lines.append("")
    return "\n".join(lines).rstrip()


async def build_subordinate_report_for_parent(
    session: AsyncSession,
    *,
    parent: OrgPrincipal,
    traffic_by_user_id: dict[int, str] | None = None,
) -> str | None:
    """Return report text, or ``None`` when parent has no reportable children."""
    shops = await list_direct_subordinate_shops(session, parent=parent)
    if not shops:
        return None
    stats = await collect_subordinate_stats(
        session, shops, traffic_by_user_id=traffic_by_user_id
    )
    title = (
        "گزارش نمایندگان"
        if is_owner_principal(parent)
        else "گزارش زیرنمایندگان"
    )
    return format_subordinate_report(stats, title=title, period_label="امروز")


async def load_owner_parent(session: AsyncSession) -> OrgPrincipal | None:
    try:
        return await ensure_owner_principal(session)
    except Exception:
        log.debug("subordinate report: owner principal unavailable", exc_info=True)
        return None
