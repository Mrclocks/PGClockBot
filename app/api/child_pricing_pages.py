"""«نمایندگان من» › «قیمت و اعتبار زیرمجموعه»: a level-1 shop prices and backs its level-2 shops."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.payg_plan_form import payg_settings_from_form
from app.db.models import ResellerProfile, ShopCreditAccount
from app.services.child_pricing import (
    ChildPlanInput,
    ChildPricingError,
    assign_child_plan,
    create_child_plan,
    delete_child_plan,
    list_child_plans,
    parent_scope,
    set_child_credit,
    update_child_plan,
)
from app.services.numbers import parse_int
from app.services.shop_credit import child_shop_ids, shop_credit_limit

PAGE = "/resellers/children-pricing"


def _back(*, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"err={quote(err)}" if err else f"ok={quote(ok)}"
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


def _number(form: Mapping[str, Any], key: str, *, optional: bool = False) -> int | None:
    raw = str(form.get(key) or "").strip()
    if not raw:
        return None if optional else 0
    try:
        value = parse_int(raw)
    except ValueError:
        raise ChildPricingError("عدد نامعتبر است") from None
    if value < 0:
        raise ChildPricingError("عدد نامعتبر است")
    return value


def plan_from_form(form: Mapping[str, Any]) -> ChildPlanInput:
    rate = int(_number(form, "price_per_gb") or 0)
    try:
        settings = payg_settings_from_form({**dict(form), "payg_fields_shown": "1", "payg_charge_upfront": "1"},
                                           price_per_gb=rate)
    except ValueError as exc:
        raise ChildPricingError(str(exc)) from exc
    assert settings is not None
    return ChildPlanInput(name=str(form.get("name") or "")[:128], basis=settings.basis, price_per_gb=rate,
                          user_price=settings.user_price, user_price_per_30d=settings.user_price_per_30d,
                          unlimited_price=settings.unlimited_price)


async def _children(session: AsyncSession, parent_shop_id: int) -> list[dict[str, Any]]:
    ids = await child_shop_ids(session, parent_shop_id)
    if not ids:
        return []
    profiles = (await session.scalars(select(ResellerProfile).where(ResellerProfile.user_id.in_(ids)))).all()
    limits = {int(a.shop_id): int(a.credit_limit or 0) for a in (await session.scalars(
        select(ShopCreditAccount).where(ShopCreditAccount.shop_id.in_(ids)))).all()}
    return [{"shop_id": int(p.user_id), "label": p.pg_admin_username or p.web_username or f"#{p.user_id}",
             "plan_id": p.child_plan_id, "credit_limit": limits.get(int(p.user_id), 0)} for p in profiles]


def register_child_pricing_pages(app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable) -> None:
    @app.get(PAGE, response_class=HTMLResponse)
    async def children_pricing(request: Request, staff: dict = Depends(require_staff),
                               session: AsyncSession = Depends(get_db)) -> Any:
        try:
            parent = parent_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        return render(request, "children_pricing.html", {
            "staff": staff, "plans": await list_child_plans(session, parent),
            "children": await _children(session, parent), "free_credit": await shop_credit_limit(session, parent),
            "flash_ok": request.query_params.get("ok"), "flash_err": request.query_params.get("err"),
        })

    @app.post(f"{PAGE}/plans")
    async def children_pricing_plan(request: Request, staff: dict = Depends(require_staff),
                                    session: AsyncSession = Depends(get_db)) -> RedirectResponse:
        try:
            parent = parent_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        try:
            await create_child_plan(session, parent, plan_from_form(await request.form()))
        except ChildPricingError as exc:
            return _back(err=str(exc))
        return _back(ok="پلن قیمت ساخته شد")

    @app.post(f"{PAGE}/plans/{{plan_id}}")
    async def children_pricing_plan_edit(plan_id: int, request: Request, staff: dict = Depends(require_staff),
                                         session: AsyncSession = Depends(get_db)) -> RedirectResponse:
        try:
            parent = parent_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        try:
            await update_child_plan(session, parent, int(plan_id), plan_from_form(await request.form()))
        except ChildPricingError as exc:
            return _back(err=str(exc))
        return _back(ok="پلن قیمت ذخیره شد؛ از هزینهٔ بعدی اعمال می‌شود")

    @app.post(f"{PAGE}/plans/{{plan_id}}/delete")
    async def children_pricing_plan_delete(plan_id: int, staff: dict = Depends(require_staff),
                                           session: AsyncSession = Depends(get_db)) -> RedirectResponse:
        try:
            parent = parent_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        try:
            await delete_child_plan(session, parent, int(plan_id))
        except ChildPricingError as exc:
            return _back(err=str(exc))
        return _back(ok="پلن قیمت حذف شد")

    @app.post(PAGE + "/{child_shop_id}")
    async def children_pricing_child(child_shop_id: int, request: Request, staff: dict = Depends(require_staff),
                                     session: AsyncSession = Depends(get_db)) -> RedirectResponse:
        try:
            parent = parent_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            await assign_child_plan(session, parent, int(child_shop_id), _number(form, "plan_id", optional=True) or None)
            await set_child_credit(session, parent, int(child_shop_id), int(_number(form, "credit_limit") or 0),
                                   actor=str(staff.get("username") or "parent"))
        except ChildPricingError as exc:
            return _back(err=str(exc))
        return _back(ok="قیمت و اعتبار زیرمجموعه ذخیره شد")
