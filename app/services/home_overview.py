"""Aggregate data for the top-level admin home dashboard."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    Payment,
    PaymentStatus,
    Plan,
    ResellerProfile,
    Ticket,
    UserService,
)
from app.services.demo_users import non_demo_customer
from app.services.pasarguard import get_pg
from app.services.service_scope import shop_services_clause
from app.services.shop_membership import shop_member_clause
from app.services.ticket_scope import ticket_shop_clause
from app.services.setup_wizard import current_setup_values

_TEHRAN = ZoneInfo("Asia/Tehran")

_ERR_NODE = frozenset({"error", "offline", "unhealthy", "disabled", "disconnected"})
_OK_NODE = frozenset({"connected", "online", "healthy", "active"})
_WARN_NODE = frozenset({"pending", "waiting", "connecting"})


def _node_tone(status: str) -> str:
    st = (status or "").strip().lower()
    if st in _OK_NODE:
        return "ok"
    if st in _ERR_NODE or "disconnect" in st or "offline" in st:
        return "err"
    if st in _WARN_NODE or "connect" in st:
        return "warn"
    return "neutral"


def _tone_class(pct: float | None) -> str:
    if pct is None:
        return "neutral"
    if pct >= 90:
        return "err"
    if pct >= 75:
        return "warn"
    return "ok"


async def check_bot_connection(token: str | None = None) -> dict[str, Any]:
    """Probe Telegram getMe for the given token only.

    Never falls back to the platform admin BOT_TOKEN. Callers that need the
    main bot must pass that token explicitly — empty/missing means unset.
    """
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "توکن تنظیم نشده", "username": None, "name": None}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = resp.json()
        if data.get("ok") and isinstance(data.get("result"), dict):
            me = data["result"]
            return {
                "ok": True,
                "error": None,
                "username": me.get("username"),
                "name": me.get("first_name"),
                "id": me.get("id"),
            }
        return {
            "ok": False,
            "error": data.get("description") or "توکن نامعتبر",
            "username": None,
            "name": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "عدم اتصال به تلگرام",
            "detail": str(exc),
            "username": None,
            "name": None,
        }


def _summarize_nodes(nodes: list | None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    connected = warn = err = 0
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        st = str(n.get("status") or n.get("connection_status") or "")
        tone = _node_tone(st)
        if tone == "ok":
            connected += 1
        elif tone == "warn":
            warn += 1
        elif tone == "err":
            err += 1
        rows.append(
            {
                "id": n.get("id"),
                "name": n.get("name") or n.get("address") or "—",
                "status": st or "—",
                "tone": tone,
            }
        )
    overall = "ok"
    if err:
        overall = "err"
    elif warn or not rows:
        overall = "warn" if rows else "neutral"
    if rows and connected == len(rows):
        overall = "ok"
    return {
        "ok": True,
        "error": None,
        "nodes": rows,
        "total": len(rows),
        "connected": connected,
        "warn": warn,
        "error_count": err,
        "overall": overall,
    }


async def bot_panel_summary(session: AsyncSession) -> dict[str, Any]:
    """Single round-trip aggregate counts for the bot dashboard.

    Platform-scoped only: shop-tenant orders/payments/tickets are excluded
    (hard shop isolation — match web payments/orders lists).
    """
    pending_expr = (
        select(func.count())
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            non_demo_customer(Payment.user_id),
            or_(
                and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id.is_(None)),
                and_(Payment.is_wallet_topup.is_(False), Order.reseller_id.is_(None)),
            ),
        )
        .scalar_subquery()
    )
    revenue_expr = (
        select(func.coalesce(func.sum(Order.amount), 0))
        .where(Order.status == "delivered", Order.reseller_id.is_(None), non_demo_customer(Order.user_id))
        .scalar_subquery()
    )
    tickets_expr = (
        select(func.count())
        .select_from(Ticket)
        .join(BotUser, BotUser.id == Ticket.user_id)
        .where(Ticket.status == "open", ticket_shop_clause(None), BotUser.is_demo.is_(False))
        .scalar_subquery()
    )
    resellers_expr = (
        select(func.count())
        .select_from(ResellerProfile)
        .where(ResellerProfile.is_active.is_(True))
        .scalar_subquery()
    )
    orders_expr = (
        select(func.count()).select_from(Order).where(Order.reseller_id.is_(None), non_demo_customer(Order.user_id)).scalar_subquery()
    )
    row = (
        await session.execute(
            select(
                select(func.count()).select_from(BotUser).where(BotUser.is_demo.is_(False)).scalar_subquery(),
                orders_expr,
                select(func.count()).select_from(UserService).where(shop_services_clause(None), non_demo_customer(UserService.bot_user_id)).scalar_subquery(),
                pending_expr,
                revenue_expr,
                tickets_expr,
                resellers_expr,
            )
        )
    ).one()
    return {
        "users": int(row[0] or 0),
        "orders": int(row[1] or 0),
        "services": int(row[2] or 0),
        "pending": int(row[3] or 0),
        "revenue": int(row[4] or 0),
        "tickets": int(row[5] or 0),
        "resellers": int(row[6] or 0),
    }


async def pg_home_bundle(*, include_nodes: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch PasarGuard summary + node status in one pass (shared nodes call)."""
    nodes_status = {
        "ok": False,
        "error": None,
        "nodes": [],
        "total": 0,
        "connected": 0,
        "warn": 0,
        "error_count": 0,
        "overall": "neutral",
    }
    summary: dict[str, Any] = {
        "ok": False,
        "error": None,
        "admins": 0,
        "groups": 0,
        "hosts": 0,
        "nodes": 0,
        "users": None,
        "version": None,
    }
    try:
        from app.services.pasarguard import is_pg_permission_denied

        pg = get_pg()
        await pg.ensure_token()

        async def _no_nodes():
            return []

        node_call = pg.get_nodes_simple() if include_nodes else _no_nodes()
        admins, groups, hosts, nodes, stats = await asyncio.gather(
            pg.get_admins_simple(),
            pg.get_groups_simple(),
            pg.get_hosts(),
            node_call,
            pg.get_system_stats(),
            return_exceptions=True,
        )
        # Token already proved reachability. 403 on admins/hosts/nodes is a
        # limited role, not a dropped connection.
        summary["ok"] = True
        if not include_nodes:
            nodes_status = {**_empty_nodes(), "ok": True, "overall": "neutral"}
            nodes = []
        elif isinstance(nodes, Exception):
            if is_pg_permission_denied(nodes):
                nodes_status["ok"] = True
                nodes_status["overall"] = "neutral"
            else:
                nodes_status["error"] = "خطا در دریافت نودها"
                nodes_status["overall"] = "err"
            nodes = []
        else:
            nodes_status = _summarize_nodes(nodes if isinstance(nodes, list) else [])

        def _count(payload: Any) -> int:
            return len(payload) if isinstance(payload, list) else 0

        summary.update(
            {
                "admins": _count(admins),
                "groups": _count(groups),
                "hosts": _count(hosts),
                "nodes": nodes_status["total"],
            }
        )
        if isinstance(stats, dict):
            for key in ("total_user", "users_active", "users", "total_users"):
                if key in stats and isinstance(stats[key], (int, float)):
                    summary["users"] = int(stats[key])
                    break
            ver = stats.get("version")
            if ver:
                summary["version"] = str(ver)
    except Exception:
        summary["error"] = "اتصال به پاسارگارد برقرار نشد"
        nodes_status["error"] = summary["error"]
        nodes_status["overall"] = "err"
    return summary, nodes_status


def _empty_host() -> dict[str, Any]:
    return {
        "ok": False,
        "cpu_percent": None,
        "cpu_cores": None,
        "memory_percent": None,
        "memory_used": None,
        "memory_total": None,
        "memory_used_text": "—",
        "memory_total_text": "—",
        "memory_ratio_text": "—",
        "cpu_tone": "neutral",
        "mem_tone": "neutral",
    }


def _empty_bot() -> dict[str, Any]:
    return {"ok": False, "error": None, "username": None, "name": None}


def empty_period_bucket() -> dict[str, int]:
    return {
        "orders": 0,
        "delivered": 0,
        "revenue": 0,
        "new_users": 0,
    }


def empty_period_stats() -> dict[str, Any]:
    return {
        "day": empty_period_bucket(),
        "week": empty_period_bucket(),
        "month": empty_period_bucket(),
    }


def _period_since_utc() -> dict[str, datetime]:
    """Today / last-7-days / last-30-days anchored on Asia/Tehran midnight."""
    now_local = datetime.now(_TEHRAN)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=6)
    month_start = day_start - timedelta(days=29)
    return {
        "day": day_start.astimezone(timezone.utc),
        "week": week_start.astimezone(timezone.utc),
        "month": month_start.astimezone(timezone.utc),
    }


async def shop_period_stats(
    session: AsyncSession, *, reseller_id: int | None = None
) -> dict[str, Any]:
    """Sales-ish totals for امروز / ۷ روز / ۳۰ روز (shop-scoped).

    Two aggregated queries (orders + users) instead of 12 sequential counts.
    """
    from sqlalchemy import case

    starts = _period_since_utc()
    out = empty_period_stats()
    day_s, week_s, month_s = starts["day"], starts["week"], starts["month"]

    if reseller_id is None:
        scope_orders = Order.reseller_id.is_(None)
        scope_users = shop_member_clause(None)
    else:
        rid = int(reseller_id)
        scope_orders = Order.reseller_id == rid
        scope_users = shop_member_clause(rid)

    def _sum_since(since, expr):
        return func.coalesce(
            func.sum(case((Order.created_at >= since, expr), else_=0)),
            0,
        )

    delivered = Order.status == "delivered"
    orders_row = (
        await session.execute(
            select(
                _sum_since(day_s, 1),
                _sum_since(day_s, case((delivered, 1), else_=0)),
                _sum_since(day_s, case((delivered, Order.amount), else_=0)),
                _sum_since(week_s, 1),
                _sum_since(week_s, case((delivered, 1), else_=0)),
                _sum_since(week_s, case((delivered, Order.amount), else_=0)),
                _sum_since(month_s, 1),
                _sum_since(month_s, case((delivered, 1), else_=0)),
                _sum_since(month_s, case((delivered, Order.amount), else_=0)),
            )
            .select_from(Order)
            .where(Order.created_at >= month_s, scope_orders, non_demo_customer(Order.user_id))
        )
    ).one()

    def _users_since(since):
        return func.coalesce(
            func.sum(case((BotUser.created_at >= since, 1), else_=0)),
            0,
        )

    users_row = (
        await session.execute(
            select(
                _users_since(day_s),
                _users_since(week_s),
                _users_since(month_s),
            )
            .select_from(BotUser)
            .where(BotUser.created_at >= month_s, scope_users, BotUser.is_demo.is_(False))
        )
    ).one()

    out["day"] = {
        "orders": int(orders_row[0] or 0),
        "delivered": int(orders_row[1] or 0),
        "revenue": int(orders_row[2] or 0),
        "new_users": int(users_row[0] or 0),
    }
    out["week"] = {
        "orders": int(orders_row[3] or 0),
        "delivered": int(orders_row[4] or 0),
        "revenue": int(orders_row[5] or 0),
        "new_users": int(users_row[1] or 0),
    }
    out["month"] = {
        "orders": int(orders_row[6] or 0),
        "delivered": int(orders_row[7] or 0),
        "revenue": int(orders_row[8] or 0),
        "new_users": int(users_row[2] or 0),
    }
    return out


def _empty_bot_summary() -> dict[str, Any]:
    return {
        "users": 0,
        "orders": 0,
        "services": 0,
        "pending": 0,
        "revenue": 0,
        "tickets": 0,
        "resellers": 0,
    }


def _empty_pg_summary() -> dict[str, Any]:
    return {
        "ok": False,
        "error": None,
        "admins": 0,
        "groups": 0,
        "hosts": 0,
        "nodes": 0,
        "users": None,
        "version": None,
    }


def _empty_nodes() -> dict[str, Any]:
    return {
        "ok": False,
        "error": None,
        "nodes": [],
        "total": 0,
        "connected": 0,
        "warn": 0,
        "error_count": 0,
        "overall": "neutral",
    }


def empty_home_overview() -> dict[str, Any]:
    """Fail-soft shell so ``home.html`` never sees missing keys / Undefined."""
    return {
        "host": _empty_host(),
        "bot": _empty_bot(),
        "nodes": _empty_nodes(),
        "bot_summary": _empty_bot_summary(),
        "pg_summary": _empty_pg_summary(),
    }


async def build_home_overview(
    session: AsyncSession, *, include_nodes: bool = True
) -> dict[str, Any]:
    """Build admin home payloads; never raise — partial failures return defaults.

    Host CPU/RAM live on bot/PG overviews now — skipped here for a faster /home.
    ``include_nodes`` is False when the live PG role has no nodes permission.
    """
    import logging

    from app.services.db_safe import rollback_quiet

    log = logging.getLogger(__name__)
    out = empty_home_overview()
    # Platform admin overview only — pass main token explicitly (no silent fallback).
    bot_task = check_bot_connection(current_setup_values().get("BOT_TOKEN"))
    bot_sum_task = bot_panel_summary(session)
    pg_task = pg_home_bundle(include_nodes=include_nodes)

    bot, bot_sum, pg_pair = await asyncio.gather(
        bot_task, bot_sum_task, pg_task, return_exceptions=True
    )

    if isinstance(bot, Exception):
        log.exception("home bot probe failed: %s", bot)
        out["bot"] = {**_empty_bot(), "error": "بررسی ربات ناموفق"}
    elif isinstance(bot, dict):
        out["bot"] = bot

    if isinstance(bot_sum, Exception):
        log.exception("home bot_panel_summary failed: %s", bot_sum)
        await rollback_quiet(session)
        out["bot_summary"] = _empty_bot_summary()
    elif isinstance(bot_sum, dict):
        out["bot_summary"] = bot_sum

    if isinstance(pg_pair, Exception):
        log.exception("home pg_home_bundle failed: %s", pg_pair)
        out["pg_summary"] = {
            **_empty_pg_summary(),
            "error": "اتصال به پاسارگارد برقرار نشد",
        }
        out["nodes"] = {**_empty_nodes(), "overall": "err", "error": out["pg_summary"]["error"]}
    elif isinstance(pg_pair, tuple) and len(pg_pair) == 2:
        pg_sum, nodes = pg_pair
        if isinstance(pg_sum, dict):
            out["pg_summary"] = pg_sum
        if isinstance(nodes, dict):
            out["nodes"] = nodes

    return out


async def reseller_shop_summary(
    session: AsyncSession, rid: int, *, ticket_statuses: tuple[str, ...] = ("open",),
) -> dict[str, int]:
    """Single round-trip aggregate counts for a reseller shop dashboard."""
    users_expr = (
        select(func.count()).select_from(BotUser).where(shop_member_clause(rid), BotUser.is_demo.is_(False)).scalar_subquery()
    )
    orders_expr = (
        select(func.count()).select_from(Order).where(Order.reseller_id == rid, non_demo_customer(Order.user_id)).scalar_subquery()
    )
    pending_expr = (
        select(func.count())
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            non_demo_customer(Payment.user_id),
            or_(
                and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id == rid),
                and_(Payment.is_wallet_topup.is_(False), Order.reseller_id == rid),
            ),
        )
        .scalar_subquery()
    )
    services_expr = (
        select(func.count())
        .select_from(UserService)
        .join(BotUser, BotUser.id == UserService.bot_user_id)
        .where(shop_services_clause(rid), BotUser.is_demo.is_(False))
        .scalar_subquery()
    )
    revenue_expr = (
        select(func.coalesce(func.sum(Order.amount), 0))
        .where(Order.status == "delivered", Order.reseller_id == rid, non_demo_customer(Order.user_id))
        .scalar_subquery()
    )
    plans_expr = (
        select(func.count())
        .select_from(Plan)
        .where(Plan.is_active.is_(True), Plan.owner_reseller_id == rid)
        .scalar_subquery()
    )
    tickets_expr = (
        select(func.count())
        .select_from(Ticket)
        .join(BotUser, BotUser.id == Ticket.user_id)
        .where(
            Ticket.status.in_(ticket_statuses), BotUser.is_demo.is_(False),
            Ticket.reseller_id == rid,
        )
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(
                users_expr,
                orders_expr,
                pending_expr,
                services_expr,
                revenue_expr,
                plans_expr,
                tickets_expr,
            )
        )
    ).one()
    return {
        "users": int(row[0] or 0),
        "orders": int(row[1] or 0),
        "pending": int(row[2] or 0),
        "services": int(row[3] or 0),
        "revenue": int(row[4] or 0),
        "plans": int(row[5] or 0),
        "tickets": int(row[6] or 0),
    }


async def admin_customer_counts(session: AsyncSession) -> dict[str, int]:
    """CRM hub counts exclude test customers while their records stay manageable."""
    users = select(func.count()).select_from(BotUser).where(BotUser.is_demo.is_(False))
    row = (await session.execute(select(
        users.scalar_subquery(),
        users.where(BotUser.is_blocked.is_(True)).scalar_subquery(),
        select(func.count()).select_from(Order).where(Order.reseller_id.is_(None), non_demo_customer(Order.user_id)).scalar_subquery(),
    ))).one()
    return {"users": int(row[0]), "blocked": int(row[1]), "orders": int(row[2])}


async def bot_dashboard_summary(session: AsyncSession) -> dict[str, int]:
    """Add the platform approval queue to the shared non-demo dashboard totals."""
    summary = await bot_panel_summary(session)
    pending_orders = await session.scalar(
        select(func.count()).select_from(Order).where(
            Order.reseller_id.is_(None), non_demo_customer(Order.user_id),
            Order.status.in_(["awaiting_approval", "paid"]),
        )
    )
    return {**summary, "pending_orders": int(pending_orders or 0)}
