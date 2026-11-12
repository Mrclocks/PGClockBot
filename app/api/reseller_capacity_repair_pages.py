"""Owner-only action: re-read the existing limits of «ظرفیت فعلی» resellers that were saved as zero."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.platform_identity import is_explicit_owner_staff
from app.services.reseller_capacity_repair import REPAIR_BATCH, repair_current_capacity_snapshots
from app.services.table_bulk import redirect_bulk

REPAIR_PATH = "/resellers/repair-capacity"
LIST_URL = "/resellers"
DENIED = "فقط مالک اصلی می‌تواند ظرفیت‌ها را از پاسارگارد دوباره بخواند"


def register_reseller_capacity_repair_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(REPAIR_PATH)
    async def repair_capacity(
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        if not is_explicit_owner_staff(staff):
            return redirect_bulk(LIST_URL, err=DENIED)
        report = await repair_current_capacity_snapshots(session)
        text = f"{report.fixed} نماینده اصلاح شد"
        if report.plan_linked:
            text += f"؛ پلن {report.plan_linked} اشتراک از پروفایل نوشته شد"
        if report.unreadable:
            text += f"؛ {report.unreadable} مورد از پاسارگارد خوانده نشد"
        if report.fixed + report.unreadable + report.plan_linked >= REPAIR_BATCH:
            text += "؛ برای بقیه دوباره بزنید"
        return redirect_bulk(LIST_URL, ok=text)
