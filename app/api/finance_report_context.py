"""Translate finance report filters and validation errors for the web page."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.finance_report_analytics import build_finance_dashboard
from app.services.finance_report_ranges import resolve_finance_range


async def build_finance_report_context(
    session: AsyncSession, *, reseller_id: int | None, query: Mapping[str, str],
) -> dict[str, Any]:
    filters = {"period": query.get("period", "week"), "start": query.get("start", ""), "end": query.get("end", "")}
    try:
        window = resolve_finance_range(**filters)
    except ValueError as exc:
        return {"report": {}, "report_error": str(exc), "report_filters": filters}
    report = await build_finance_dashboard(session, reseller_id=reseller_id, window=window)
    return {
        "report": report, "report_period": window.period,
        "report_filters": {"period": window.period, "start": window.start_input, "end": window.end_input},
    }
