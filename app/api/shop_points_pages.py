"""Shop staff change a customer's loyalty points by hand (the loyalty transactions tab)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.shop_manual_adjust import ManualAdjustError
from app.services.shop_points_adjust import adjust_shop_points, parse_points_request, points_adjust_shop

SHOP_POINTS_PATH = "/loyalty/shop-adjust"
RETURN_TO = "/loyalty?tab=transactions"


def register_shop_points_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(SHOP_POINTS_PATH)
    async def shop_points_adjust(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            points_adjust_shop(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            change = await adjust_shop_points(
                session,
                staff=staff,
                request=parse_points_request(
                    raw_customer_id=form.get("user_id"), raw_points=form.get("points"),
                    direction=form.get("mode"), reason=form.get("reason"), request_key=form.get("nonce"),
                ),
            )
        except ManualAdjustError as exc:
            return RedirectResponse(f"{RETURN_TO}&err={quote(str(exc))}", status_code=303)
        verb = "افزوده شد" if change.direction == "credit" else "کسر شد"
        message = f"{change.points:,} امتیاز {verb} · مانده: {change.balance_after:,}"
        return RedirectResponse(f"{RETURN_TO}&ok={quote(message)}", status_code=303)
