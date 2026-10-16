"""Finance reports: day / week / month shop-scoped stats for web + Telegram.

Security: every query takes an explicit ``reseller_id`` (``None`` = Owner
platform shop). Never fall through to another tenant's rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    DeliveryFailure,
    Order,
    Payment,
    PaymentStatus,
    Plan,
    Ticket,
    UserService,
)
from app.services.demo_users import non_demo_customer
from app.services.formatting import format_toman
from app.services.home_overview import _period_since_utc, shop_period_stats
from app.services.users_ops import DEFAULT_EXPIRE_DAYS
from app.services.service_scope import shop_services_clause
from app.services.shop_membership import shop_member_clause
from app.services.ticket_scope import ticket_shop_clause

ReportPeriod = Literal["day", "week", "month"]
VALID_PERIODS: frozenset[str] = frozenset({"day", "week", "month"})
_TEHRAN = ZoneInfo("Asia/Tehran")

PERIOD_LABELS_FA = {
    "day": "امروز",
    "week": "۷ روز",
    "month": "۳۰ روز",
}


def normalize_report_period(raw: str | None) -> ReportPeriod:
    key = (raw or "week").strip().lower()
    if key in VALID_PERIODS:
        return key  # type: ignore[return-value]
    return "week"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def empty_ops_snapshot() -> dict[str, int]:
    return {
        "total_users": 0,
        "blocked_users": 0,
        "pending_receipts": 0,
        "open_tickets": 0,
        "delivery_failures": 0,
        "expiring_services": 0,
        "low_volume_services": 0,
        "no_service_users": 0,
    }


async def shop_ops_snapshot(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    expire_days: int = DEFAULT_EXPIRE_DAYS,
) -> dict[str, int]:
    """Point-in-time ops counts for the shop (not period-scoped except expire window)."""
    expire_days = max(1, min(30, int(expire_days or DEFAULT_EXPIRE_DAYS)))
    out = empty_ops_snapshot()

    if reseller_id is None:
        user_scope = shop_member_clause(None)
        fail_scope = DeliveryFailure.reseller_id.is_(None)
        pay_scope = or_(
            and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id.is_(None)),
            and_(Payment.is_wallet_topup.is_(False), Order.reseller_id.is_(None)),
        )
    else:
        rid = int(reseller_id)
        user_scope = shop_member_clause(rid)
        fail_scope = DeliveryFailure.reseller_id == rid
        pay_scope = or_(
            and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id == rid),
            and_(Payment.is_wallet_topup.is_(False), Order.reseller_id == rid),
        )

    users_row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(case((BotUser.is_blocked.is_(True), 1), else_=0)), 0),
            )
            .select_from(BotUser)
            .where(user_scope, BotUser.is_demo.is_(False))
        )
    ).one()
    out["total_users"] = int(users_row[0] or 0)
    out["blocked_users"] = int(users_row[1] or 0)

    # Users with zero UserService rows (shop customers only — same list rules)
    has_svc = (
        select(UserService.id)
        .where(UserService.bot_user_id == BotUser.id, shop_services_clause(reseller_id))
        .correlate(BotUser)
        .exists()
    )
    no_svc = int(
        (
            await session.execute(
                select(func.count())
                .select_from(BotUser)
                .where(user_scope, BotUser.is_demo.is_(False), ~has_svc)
            )
        ).scalar()
        or 0
    )
    out["no_service_users"] = no_svc

    pending_q = (
        select(func.count())
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .outerjoin(BotUser, BotUser.id == Payment.user_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            pay_scope,
            BotUser.is_demo.is_(False),
        )
    )
    out["pending_receipts"] = int((await session.execute(pending_q)).scalar() or 0)

    tickets_q = (
        select(func.count())
        .select_from(Ticket)
        .join(BotUser, BotUser.id == Ticket.user_id)
        .where(Ticket.status == "open", BotUser.is_demo.is_(False))
    )
    tickets_q = tickets_q.where(ticket_shop_clause(reseller_id))
    out["open_tickets"] = int((await session.execute(tickets_q)).scalar() or 0)

    fail_q = (
        select(func.count())
        .select_from(DeliveryFailure)
        .join(Order, Order.id == DeliveryFailure.order_id)
        .where(DeliveryFailure.resolved_at.is_(None), fail_scope, non_demo_customer(Order.user_id))
    )
    out["delivery_failures"] = int((await session.execute(fail_q)).scalar() or 0)

    low_q = (
        select(func.count())
        .select_from(UserService)
        .join(BotUser, BotUser.id == UserService.bot_user_id)
        .where(UserService.notified_traffic.is_(True), shop_services_clause(reseller_id), BotUser.is_demo.is_(False))
    )
    out["low_volume_services"] = int((await session.execute(low_q)).scalar() or 0)

    # Expiring approx — same window as action center / users ops
    from datetime import timedelta

    now = _utcnow()
    cutoff = now + timedelta(days=expire_days)
    max_days = int(
        (
            await session.execute(
                select(func.coalesce(func.max(Plan.duration_days), 0)).select_from(Plan)
            )
        ).scalar()
        or 0
    )
    expiring = 0
    if max_days > 0:
        scan_since = now - timedelta(days=max_days)
        rows = (
            await session.execute(
                select(UserService.created_at, Plan.duration_days)
                .select_from(UserService)
                .join(Plan, Plan.id == UserService.plan_id)
                .join(BotUser, BotUser.id == UserService.bot_user_id)
                .where(
                    UserService.plan_id.is_not(None),
                    Plan.duration_days.is_not(None),
                    UserService.created_at.is_not(None),
                    UserService.created_at >= scan_since,
                    shop_services_clause(reseller_id),
                    BotUser.is_demo.is_(False),
                )
            )
        ).all()
        for created, days in rows:
            if not created or not days:
                continue
            try:
                exp = created
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                exp = exp + timedelta(days=int(days))
            except Exception:
                continue
            if now <= exp <= cutoff:
                expiring += 1
    out["expiring_services"] = expiring
    return out


async def shop_payment_method_breakdown(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    since: datetime,
) -> list[dict[str, Any]]:
    """Delivered-order payment methods in [since, now] for the shop."""
    if reseller_id is None:
        scope = Order.reseller_id.is_(None)
    else:
        scope = Order.reseller_id == int(reseller_id)
    # Label + group_by(same label) — required for PostgreSQL GROUP BY rules
    # (SQLite is looser and hid this). Do not repeat coalesce() in group_by.
    method_col = func.coalesce(Order.payment_method, "—").label("pay_method")
    rows = (
        await session.execute(
            select(
                method_col,
                func.count(),
                func.coalesce(func.sum(Order.amount), 0),
            )
            .where(
                Order.status == "delivered",
                Order.created_at >= since,
                scope,
                non_demo_customer(Order.user_id),
            )
            .group_by(method_col)
            .order_by(func.count().desc())
        )
    ).all()
    labels = {"card": "کارت", "wallet": "کیف پول", "stars": "Stars", "—": "نامشخص"}
    out = []
    for method, count, amount in rows:
        key = str(method or "—")
        out.append(
            {
                "method": key,
                "label": labels.get(key, key),
                "count": int(count or 0),
                "amount": int(amount or 0),
            }
        )
    return out


def enrich_period_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    """Add derived fields (avg order) without mutating callers' empty templates badly."""
    row = dict(bucket)
    delivered = int(row.get("delivered") or 0)
    revenue = int(row.get("revenue") or 0)
    row["avg_order"] = int(revenue // delivered) if delivered > 0 else 0
    return row


async def build_finance_report(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    period: ReportPeriod | str = "week",
    expire_days: int = DEFAULT_EXPIRE_DAYS,
) -> dict[str, Any]:
    """Full report payload for web panel + Telegram."""
    period_key = normalize_report_period(str(period))
    periods = await shop_period_stats(session, reseller_id=reseller_id)
    for key in ("day", "week", "month"):
        periods[key] = enrich_period_bucket(periods.get(key) or {})

    ops = await shop_ops_snapshot(
        session, reseller_id=reseller_id, expire_days=expire_days
    )
    starts = _period_since_utc()
    methods = await shop_payment_method_breakdown(
        session, reseller_id=reseller_id, since=starts[period_key]
    )
    active = periods[period_key]
    now_local = datetime.now(_TEHRAN)

    return {
        "period": period_key,
        "period_label": PERIOD_LABELS_FA[period_key],
        "periods": periods,
        "active": active,
        "ops": ops,
        "payment_methods": methods,
        "generated_at": now_local.strftime("%Y/%m/%d %H:%M"),
        "scope": "platform" if reseller_id is None else "shop",
    }


def format_finance_report_telegram(
    report: dict[str, Any],
    *,
    currency: str = "تومان",
) -> str:
    """Compact categorized Telegram HTML for admin/reseller."""
    import html as html_mod

    period = report.get("period") or "week"
    label = html_mod.escape(str(report.get("period_label") or PERIOD_LABELS_FA.get(period, "")))
    active = report.get("active") or {}
    ops = report.get("ops") or {}
    methods = report.get("payment_methods") or []

    lines = [
        f"📊 <b>گزارش مالی — {label}</b>",
        f"<i>{html_mod.escape(str(report.get('generated_at') or ''))}</i>",
        "",
        "— فروش —",
        f"💰 درآمد: <b>{format_toman(int(active.get('revenue') or 0), currency)}</b>",
        f"🛒 فروش موفق: <b>{int(active.get('delivered') or 0)}</b>",
        f"📝 سفارش ثبت‌شده: <b>{int(active.get('orders') or 0)}</b>",
        f"📐 میانگین فروش: <b>{format_toman(int(active.get('avg_order') or 0), currency)}</b>",
        "",
        "— کاربران —",
        f"👤 کل: <b>{int(ops.get('total_users') or 0)}</b>"
        f" · جدید: <b>{int(active.get('new_users') or 0)}</b>",
        f"🚫 مسدود: <b>{int(ops.get('blocked_users') or 0)}</b>"
        f" · بدون سرویس: <b>{int(ops.get('no_service_users') or 0)}</b>",
        "",
        "— عملیات —",
        f"⏳ رسید معلق: <b>{int(ops.get('pending_receipts') or 0)}</b>",
        f"🎫 تیکت باز: <b>{int(ops.get('open_tickets') or 0)}</b>",
        f"📦 تحویل ناموفق: <b>{int(ops.get('delivery_failures') or 0)}</b>",
        f"⏰ نزدیک انقضا: <b>{int(ops.get('expiring_services') or 0)}</b>"
        f" · 📉 حجم کم: <b>{int(ops.get('low_volume_services') or 0)}</b>",
    ]
    if methods:
        lines.append("")
        lines.append("— روش پرداخت (فروش موفق) —")
        for m in methods[:5]:
            lines.append(
                f"• {html_mod.escape(str(m.get('label') or m.get('method') or '—'))}: "
                f"{int(m.get('count') or 0)} · "
                f"{format_toman(int(m.get('amount') or 0), currency)}"
            )
    # Compact comparison footer
    periods = report.get("periods") or {}
    day = periods.get("day") or {}
    week = periods.get("week") or {}
    month = periods.get("month") or {}
    lines.extend(
        [
            "",
            "— مقایسه درآمد —",
            f"امروز {format_toman(int(day.get('revenue') or 0), currency)} · "
            f"۷روز {format_toman(int(week.get('revenue') or 0), currency)} · "
            f"۳۰روز {format_toman(int(month.get('revenue') or 0), currency)}",
        ]
    )
    return "\n".join(lines)


def finance_report_period_keyboard_rows(period: str) -> list[list[dict[str, str]]]:
    """Lightweight row descriptors for bot inline keyboards."""
    cur = normalize_report_period(period)
    out = []
    row = []
    for key, fa in PERIOD_LABELS_FA.items():
        mark = "· " if key == cur else ""
        row.append({"text": f"{mark}{fa}", "period": key})
    out.append(row)
    return out
