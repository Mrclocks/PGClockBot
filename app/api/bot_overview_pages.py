"""Bot overview (/dashboard) and its live server meters (/dashboard/metrics)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import bot_overview as overview
from app.services.dashboard_trend import empty_trend, sales_trend
from app.services.db_safe import rollback_quiet
from app.services.host_gauges import empty_host_gauges, gauges_json, local_host_gauges
from app.services.job_metrics import job_metrics
from app.services.resource_cost import HEAVY_LABEL, job_usage_rows
from app.services.shop_scope import empty_shop_stats
from app.services.delivery_failures import count_stuck_paid_orders

logger = logging.getLogger(__name__)

TREND_DAYS = 7


async def _local_host() -> dict:
    host = await asyncio.to_thread(local_host_gauges, wait_cpu=0.0)
    if host.get("cpu_percent") is None:
        host = await asyncio.to_thread(local_host_gauges, wait_cpu=0.12)
    return host


async def _figures(session: AsyncSession, viewer: overview.Viewer, period: str) -> dict:
    """Everything the page draws for one shop; a failed part shows empty, never an error page."""
    totals = await overview.shop_totals(session, viewer)
    try:
        kpis = await overview.period_kpis(session, reseller_id=viewer.shop_id, period=period)
        deliveries = await count_stuck_paid_orders(session, reseller_id=viewer.shop_id)
        trend = await sales_trend(session, reseller_id=viewer.shop_id, days=TREND_DAYS)
        payments, orders = await overview.recent_rows(session, viewer)
    except Exception:
        logger.exception("bot overview figures failed shop=%s", viewer.shop_id)
        await rollback_quiet(session)
        empty = overview.PeriodFigures(revenue=0, delivered=0, new_users=0)
        kpis = overview.PeriodKpis(period=period, current=empty, previous=empty)
        deliveries, trend, payments, orders = 0, empty_trend(TREND_DAYS), [], []
    bot = await overview.bot_status(await overview.shop_bot_token(session, viewer))
    return {
        "stats": totals,
        "kpis": kpis,
        "deliveries": deliveries,
        "trend": trend,
        "recent_payments": payments,
        "recent_orders": orders,
        "bot": bot,
    }


def register_bot_overview_pages(app, *, render, require_perm, require_admin, get_db) -> None:
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        staff: dict = Depends(require_perm("dashboard")),
        session: AsyncSession = Depends(get_db),
    ):
        viewer = overview.viewer_of(staff)
        period = overview.parse_period(request.query_params.get("period"))
        ctx = {
            "staff": staff,
            "viewer": viewer,
            "period": period,
            "period_labels": overview.PERIOD_LABELS,
            "compare_label": overview.COMPARE_LABELS[period],
            "host_gauges": None,
            "job_usage": [],
            "bot_setup_needed": False,
            "stats": empty_shop_stats(),
        }
        if viewer.has_shop and await overview.shop_needs_bot(session, viewer):
            ctx["bot_setup_needed"] = True
            return render(request, "dashboard.html", ctx)
        if viewer.has_shop:
            ctx.update(await _figures(session, viewer, period))
        if viewer.has_shop and viewer.shop_id is None:
            try:
                ctx["host_gauges"] = await asyncio.to_thread(local_host_gauges, wait_cpu=0.0)
            except Exception:
                logger.warning("bot host gauges unavailable", exc_info=True)
                ctx["host_gauges"] = empty_host_gauges()
            # Background jobs of this process: the server owner sees them next to the server meters.
            ctx["job_usage"] = job_usage_rows(job_metrics.snapshot())
            ctx["heavy_label"] = HEAVY_LABEL
        return render(request, "dashboard.html", ctx)

    @app.get("/dashboard/metrics")
    async def dashboard_host_metrics_json(staff: dict = Depends(require_admin)):
        """CPU/RAM of the bot's own server: the platform owner only (``require_admin``)."""
        return JSONResponse(gauges_json(await _local_host()), headers={"Cache-Control": "private, no-store"})
