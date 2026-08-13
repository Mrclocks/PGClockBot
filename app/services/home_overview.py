"""Aggregate data for the top-level admin home dashboard."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    Payment,
    PaymentStatus,
    ResellerProfile,
    Ticket,
    UserService,
)
from app.services.host_metrics import host_metrics
from app.services.pasarguard import get_pg
from app.services.setup_wizard import current_setup_values

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
    from sqlalchemy import or_

    pending_expr = (
        select(func.count())
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            or_(
                Payment.is_wallet_topup.is_(True),
                Order.reseller_id.is_(None),
            ),
        )
        .scalar_subquery()
    )
    revenue_expr = (
        select(func.coalesce(func.sum(Order.amount), 0))
        .where(Order.status == "delivered", Order.reseller_id.is_(None))
        .scalar_subquery()
    )
    tickets_expr = (
        select(func.count())
        .select_from(Ticket)
        .join(BotUser, BotUser.id == Ticket.user_id)
        .where(Ticket.status == "open", BotUser.reseller_id.is_(None))
        .scalar_subquery()
    )
    resellers_expr = (
        select(func.count())
        .select_from(ResellerProfile)
        .where(ResellerProfile.is_active.is_(True))
        .scalar_subquery()
    )
    orders_expr = (
        select(func.count()).select_from(Order).where(Order.reseller_id.is_(None)).scalar_subquery()
    )
    row = (
        await session.execute(
            select(
                select(func.count()).select_from(BotUser).scalar_subquery(),
                orders_expr,
                select(func.count()).select_from(UserService).scalar_subquery(),
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


async def pg_nodes_pulse() -> dict[str, Any]:
    """Lite PasarGuard probe for home pulse — nodes status only (no admin/group/host lists)."""
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
    try:
        pg = get_pg()
        nodes = await pg.get_nodes_simple()
        if isinstance(nodes, Exception):
            nodes_status["error"] = str(nodes) or "خطا در دریافت نودها"
            nodes_status["overall"] = "err"
        else:
            nodes_status = _summarize_nodes(nodes if isinstance(nodes, list) else [])
    except Exception as exc:
        nodes_status["error"] = str(exc) or "اتصال به پاسارگارد برقرار نشد"
        nodes_status["overall"] = "err"
    return nodes_status


async def pg_home_bundle() -> tuple[dict[str, Any], dict[str, Any]]:
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
        pg = get_pg()
        admins, groups, hosts, nodes, stats = await asyncio.gather(
            pg.get_admins_simple(),
            pg.get_groups_simple(),
            pg.get_hosts(),
            pg.get_nodes_simple(),
            pg.get_system_stats(),
            return_exceptions=True,
        )
        if isinstance(nodes, Exception):
            nodes_status["error"] = str(nodes) or "خطا در دریافت نودها"
            nodes_status["overall"] = "err"
            nodes = []
        else:
            nodes_status = _summarize_nodes(nodes if isinstance(nodes, list) else [])

        if any(isinstance(x, Exception) for x in (admins, groups, hosts)):
            errs = [str(x) for x in (admins, groups, hosts) if isinstance(x, Exception)]
            summary["error"] = errs[0] if errs else "خطا در دریافت آمار پاسارگارد"
        else:
            summary.update(
                {
                    "ok": True,
                    "admins": len(admins or []),
                    "groups": len(groups or []),
                    "hosts": len(hosts or []),
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
            elif isinstance(stats, Exception):
                # counts still usable even if /system fails
                pass
    except Exception as exc:
        summary["error"] = str(exc) or "اتصال به پاسارگارد برقرار نشد"
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
    session: AsyncSession, *, lite: bool = False
) -> dict[str, Any]:
    """Build admin home payloads; never raise — partial failures return defaults.

    ``lite=True`` (pulse dashboard): skip heavy PasarGuard admin/group/host lists;
    only host metrics, bot probe, shop KPIs, and node status.
    """
    import logging

    from app.services.db_safe import rollback_quiet

    log = logging.getLogger(__name__)
    out = empty_home_overview()
    metrics_task = asyncio.to_thread(host_metrics, wait_cpu=0.0)
    # Platform admin overview only — pass main token explicitly (no silent fallback).
    bot_task = check_bot_connection(current_setup_values().get("BOT_TOKEN"))
    bot_sum_task = bot_panel_summary(session)
    if lite:
        nodes_task = pg_nodes_pulse()
        metrics, bot, bot_sum, nodes = await asyncio.gather(
            metrics_task, bot_task, bot_sum_task, nodes_task, return_exceptions=True
        )
        pg_pair = None
    else:
        pg_task = pg_home_bundle()
        metrics, bot, bot_sum, pg_pair = await asyncio.gather(
            metrics_task, bot_task, bot_sum_task, pg_task, return_exceptions=True
        )
        nodes = None

    if isinstance(metrics, Exception):
        log.exception("home host_metrics failed: %s", metrics)
    elif isinstance(metrics, dict):
        cpu = metrics.get("cpu_percent")
        mem_pct = metrics.get("memory_percent")
        out["host"] = {
            **_empty_host(),
            **metrics,
            "cpu_tone": _tone_class(cpu if isinstance(cpu, (int, float)) else None),
            "mem_tone": _tone_class(mem_pct if isinstance(mem_pct, (int, float)) else None),
        }

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

    if lite:
        if isinstance(nodes, Exception):
            log.exception("home pg_nodes_pulse failed: %s", nodes)
            out["nodes"] = {
                **_empty_nodes(),
                "overall": "err",
                "error": "اتصال به پاسارگارد برقرار نشد",
            }
        elif isinstance(nodes, dict):
            out["nodes"] = nodes
        # pg_summary unused on pulse home — keep empty ok shell
        out["pg_summary"] = {**_empty_pg_summary(), "unchecked": True}
    elif isinstance(pg_pair, Exception):
        log.exception("home pg_home_bundle failed: %s", pg_pair)
        out["pg_summary"] = {
            **_empty_pg_summary(),
            "error": "اتصال به پاسارگارد برقرار نشد",
        }
        out["nodes"] = {**_empty_nodes(), "overall": "err", "error": out["pg_summary"]["error"]}
    elif isinstance(pg_pair, tuple) and len(pg_pair) == 2:
        pg_sum, nodes_st = pg_pair
        if isinstance(pg_sum, dict):
            out["pg_summary"] = pg_sum
        if isinstance(nodes_st, dict):
            out["nodes"] = nodes_st

    return out
