"""Top-level overall dashboard (outside Bot / PasarGuard menus)."""

from __future__ import annotations

import asyncio

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Payment, PaymentStatus, Plan, Ticket, UserService
from app.services.host_metrics import host_metrics
from app.services.home_overview import _tone_class, build_home_overview
from app.services.shop_scope import empty_shop_stats, is_platform_admin, shop_owner_id


async def _reseller_shop_stats(session: AsyncSession, rid: int) -> dict[str, int]:
    """Single round-trip aggregate counts for a reseller shop dashboard."""
    users_expr = (
        select(func.count()).select_from(BotUser).where(BotUser.reseller_id == rid).scalar_subquery()
    )
    orders_expr = (
        select(func.count()).select_from(Order).where(Order.reseller_id == rid).scalar_subquery()
    )
    pending_expr = (
        select(func.count())
        .select_from(Payment)
        .join(BotUser, BotUser.id == Payment.user_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            BotUser.reseller_id == rid,
        )
        .scalar_subquery()
    )
    services_expr = (
        select(func.count())
        .select_from(UserService)
        .join(BotUser, BotUser.id == UserService.bot_user_id)
        .where(BotUser.reseller_id == rid)
        .scalar_subquery()
    )
    revenue_expr = (
        select(func.coalesce(func.sum(Order.amount), 0))
        .where(Order.status == "delivered", Order.reseller_id == rid)
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
        .where(Ticket.status == "open", BotUser.reseller_id == rid)
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


def register_home_pages(app, *, render, require_admin, require_staff, get_db):
    @app.get("/home", response_class=HTMLResponse)
    async def home_dashboard(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        # Platform admin: server + both panels.
        if is_platform_admin(staff):
            overview = await build_home_overview(session)
            update = None
            try:
                from app.services.updates import check_github_update

                update = await check_github_update(force=False)
            except Exception:
                update = None
            from app.api.panel_tickets_pages import panel_ticket_dashboard_alert

            ticket_alert = await panel_ticket_dashboard_alert(
                session,
                staff,
                unread=getattr(request.state, "panel_tickets_unread", None),
            )
            return render(
                request,
                "home.html",
                {
                    "staff": staff,
                    "overview": overview,
                    "update": update,
                    "ticket_alert": ticket_alert,
                },
            )

        # Reseller / sub-admin web dashboard: bot + PasarGuard summaries.
        rid = shop_owner_id(staff)
        if not rid:
            if staff.get("pg_permissions"):
                return RedirectResponse("/pg", status_code=303)
            return RedirectResponse("/security", status_code=303)

        from app.config import get_settings
        from app.db.models import ResellerProfile
        from app.services.home_overview import check_bot_connection
        from app.services.resellers import bot_needs_setup

        profile = (
            await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == int(rid)))
        ).scalar_one_or_none()
        bot_setup_needed = bot_needs_setup(profile)
        stats = await _reseller_shop_stats(session, int(rid)) if not bot_setup_needed else empty_shop_stats()
        # Tenant bot only — never probe platform BOT_TOKEN (empty must stay unset).
        bot_token = ((profile.bot_token if profile else None) or "").strip()
        main_token = (get_settings().bot_token or "").strip()
        if not bot_token or (main_token and bot_token == main_token):
            bot = {
                "ok": False,
                "error": "توکن تنظیم نشده" if not bot_token else "توکن نامعتبر",
                "username": None,
                "name": None,
            }
        else:
            bot = await check_bot_connection(bot_token)

        pg_limits = None
        if staff.get("pg_admin_username"):
            from app.services.pg_overview import build_reseller_pg_overview

            ov = await build_reseller_pg_overview(staff, session=session)
            if ov.get("ready"):
                pg_limits = ov

        from app.api.panel_tickets_pages import panel_ticket_dashboard_alert

        ticket_alert = await panel_ticket_dashboard_alert(
            session,
            staff,
            unread=getattr(request.state, "panel_tickets_unread", None),
        )
        billing_card = None
        if profile is not None:
            from app.services.billing import is_billing_enabled, is_payg
            from app.services.formatting import format_toman

            if is_payg(profile) and await is_billing_enabled(session):
                billing_card = {
                    "balance": int(profile.billing_balance or 0),
                    "balance_fa": format_toman(int(profile.billing_balance or 0)),
                    "mode": "payg",
                }
        return render(
            request,
            "reseller_home.html",
            {
                "staff": staff,
                "stats": stats,
                "pg_limits": pg_limits,
                "bot_setup_needed": bot_setup_needed,
                "bot": bot,
                "ticket_alert": ticket_alert,
                "billing_card": billing_card,
            },
        )

    @app.get("/home/metrics")
    async def home_metrics_json(staff: dict = Depends(require_admin)):
        # Reuse prior CPU sample when polling (wait_cpu=0); sample off the event loop.
        metrics = await asyncio.to_thread(host_metrics, wait_cpu=0.0)
        if metrics.get("cpu_percent") is None:
            metrics = await asyncio.to_thread(host_metrics, wait_cpu=0.12)
        cpu = metrics.get("cpu_percent")
        mem_pct = metrics.get("memory_percent")
        return JSONResponse(
            {
                "cpu_percent": cpu,
                "memory_percent": mem_pct,
                "memory_used_text": metrics.get("memory_used_text"),
                "memory_total_text": metrics.get("memory_total_text"),
                "cpu_tone": _tone_class(cpu if isinstance(cpu, (int, float)) else None),
                "mem_tone": _tone_class(mem_pct if isinstance(mem_pct, (int, float)) else None),
            }
        )
