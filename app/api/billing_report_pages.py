"""Owner-only «مالی» › «نمایندگان PAYG» report and its CSV export."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.billing_reports import rows_csv, shop_finance_rows

PAGE = "/finance/payg"


def _month_start() -> datetime:
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def register_billing_report_pages(app: FastAPI, *, render: Callable, require_admin: Callable, get_db: Callable) -> None:
    @app.get(PAGE, response_class=HTMLResponse)
    async def payg_report(request: Request, staff: dict = Depends(require_admin),
                          session: AsyncSession = Depends(get_db)) -> Any:
        rows = await shop_finance_rows(session, month_start=_month_start())
        # Owner admins reach every finance tab, so the shared tab strip shows them all.
        return render(request, "payg_report.html",
                      {"staff": staff, "rows": rows, "can_orders": True, "can_payments": True})

    @app.get(f"{PAGE}.csv")
    async def payg_report_csv(staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)) -> Response:
        rows = await shop_finance_rows(session, month_start=_month_start())
        return Response(
            content="﻿" + rows_csv(rows), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="payg-shops.csv"', "Cache-Control": "no-store"},
        )
