"""Staff pages for configuring shop-scoped random checkout discounts."""
from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.customer_features import redirect
from app.services.random_discounts import (
    RandomDiscountInput,
    create_random_discount,
    random_discount_dashboard,
    random_discount_scope,
    stop_random_discount,
)
from app.services.shop_scope import ShopScopeError


def authorized_scope(staff: dict) -> int | None:
    try:
        return random_discount_scope(staff)
    except (PermissionError, ShopScopeError):
        raise HTTPException(403, "دسترسی ندارید") from None


def parse_form(form: Mapping[str, object]) -> RandomDiscountInput:
    mode = str(form.get("mode", ""))
    try:
        return RandomDiscountInput(
            title=str(form.get("title", "")), mode=mode,
            reduction_kind=str(form.get("reduction_kind", "")),
            minimum=int(str(form.get("minimum", ""))),
            maximum=int(str(form.get("maximum", ""))),
            step=int(str(form.get("step", ""))),
            window_minutes=int(str(form.get("window_minutes", ""))),
            code=str(form.get("code", "")) if mode == "code" else None,
            request_key=str(form.get("request_key", "")),
        )
    except (TypeError, ValueError):
        raise ValueError("بازه، گام یا مدت جلوگیری از تکرار نامعتبر است") from None


def register_random_discounts(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable,
) -> None:
    @app.get("/random-discounts")
    async def random_discount_page(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> Response:
        authorized_scope(staff)
        try:
            raw = request.query_params.get("before")
            data = await random_discount_dashboard(session, staff, before=int(raw) if raw else None)
        except ValueError:
            raise HTTPException(400, "صفحه نامعتبر است") from None
        return render(request, "random_discounts.html", {
            "staff": staff, "discounts": data.discounts, "next_before": data.next_before,
            "request_key": secrets.token_hex(16),
        })

    @app.post("/random-discounts")
    async def random_discount_create(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> Response:
        authorized_scope(staff)
        try:
            await create_random_discount(session, staff, parse_form(await request.form()))
        except ValueError as exc:
            await session.rollback()
            return redirect("/random-discounts", str(exc))
        return redirect("/random-discounts")

    @app.post("/random-discounts/{discount_id}/stop")
    async def random_discount_stop(
        discount_id: int, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> Response:
        authorized_scope(staff)
        try:
            await stop_random_discount(session, staff, discount_id)
        except ValueError as exc:
            await session.rollback()
            return redirect("/random-discounts", str(exc))
        return redirect("/random-discounts")
