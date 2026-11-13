"""Plan categories + customer service-addon packs (web panel).

Both resources are shop-scoped via ``require_catalog_owner_id`` / owned getters —
platform Owner writes ``owner_reseller_id IS NULL``; resellers write only their shop.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.numbers import to_fa_digits
from app.services.service_addons import PackRow
from app.services.shop_scope import ShopScopeError



def pack_rows_from_form(amounts: list[float], prices: list[int], styles: list[str]) -> list[PackRow]:
    """Pair the repeated amount/price/colour fields of the pack form into rows."""
    if len(prices) != len(amounts) or len(styles) not in (0, len(amounts)):
        raise ValueError("ردیف‌های بسته کامل نیست")
    colours = styles or ["inherit"] * len(amounts)
    return [PackRow(amount=a, price=p, button_style=c) for a, p, c in zip(amounts, prices, colours, strict=True)]


def packs_saved_message(count: int) -> str:
    if count == 1:
        return "بسته افزونه ذخیره شد"
    return f"{to_fa_digits(str(count))} بسته افزونه ذخیره شد"

def register_plan_catalog_extras(app, *, require_perm, get_db):
    # —— Plan categories ——

    @app.post("/plans/categories")
    async def plans_category_create(
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        sort_order: int = Form(0),
        audience: str = Form("users"),
        button_style: str = Form("inherit"),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.button_styles import parse_plan_button_style_form
        from app.services.plan_categories import create_category

        try:
            style = parse_plan_button_style_form(
                {"button_style": button_style}, field="button_style"
            )
            # parse returns None for inherit; distinguish explicit white "" via form
            raw = str(button_style).strip().lower()
            if raw == "":
                style = ""
            await create_category(
                session,
                staff,
                name=name,
                description=description,
                sort_order=sort_order,
                audience=audience,
                button_style=style,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?categories=1&err={quote(str(e))}", status_code=303
            )
        return RedirectResponse(
            f"/plans?categories=1&ok={quote('برچسب دسته ذخیره شد')}",
            status_code=303,
        )

    @app.post("/plans/categories/{category_id}/toggle")
    async def plans_category_toggle(
        category_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plan_categories import get_owned_category

        cat = await get_owned_category(session, category_id, staff)
        if not cat:
            return RedirectResponse(
                f"/plans?categories=1&err={quote('برچسب دسته یافت نشد')}",
                status_code=303,
            )
        cat.is_active = not cat.is_active
        await session.commit()
        return RedirectResponse("/plans?categories=1", status_code=303)

    @app.post("/plans/categories/{category_id}/delete")
    async def plans_category_delete(
        category_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plan_categories import delete_category

        try:
            await delete_category(session, staff, category_id)
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?categories=1&err={quote(str(e))}", status_code=303
            )
        return RedirectResponse(
            f"/plans?categories=1&ok={quote('برچسب دسته حذف شد')}",
            status_code=303,
        )

    @app.post("/plans/categories/{category_id}/edit")
    async def plans_category_edit(
        category_id: int,
        name: str = Form(...),
        description: str = Form(""),
        sort_order: int = Form(0),
        audience: str = Form("users"),
        button_style: str = Form("inherit"),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.button_styles import parse_plan_button_style_form
        from app.services.plan_categories import update_category

        try:
            style = parse_plan_button_style_form(
                {"button_style": button_style}, field="button_style"
            )
            raw = str(button_style).strip().lower()
            if raw == "":
                style = ""
            await update_category(
                session,
                staff,
                category_id,
                name=name,
                description=description,
                sort_order=sort_order,
                audience=audience,
                button_style=style,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?categories=1&err={quote(str(e))}", status_code=303
            )
        return RedirectResponse(
            f"/plans?categories=1&ok={quote('برچسب دسته به‌روزرسانی شد')}",
            status_code=303,
        )

    # —— Service addon packs ——

    @app.post("/plans/addons")
    async def plans_addon_create(
        request: Request,
        kind: str = Form(...),
        amount: list[float] = Form(...),
        price: list[int] = Form(...),
        button_style: list[str] = Form([]),
        name: str = Form(""),
        description: str = Form(""),
        sort_order: int = Form(0),
        category_id: str = Form(""),
        plan_id: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.service_addons import create_packs

        plan_raw = str(plan_id or "").strip()
        resolved_plan = int(plan_raw) if plan_raw.isdigit() else None
        try:
            packs = await create_packs(
                session,
                staff,
                kind=kind,
                rows=pack_rows_from_form(amount, price, button_style),
                name=name,
                description=description,
                sort_order=sort_order,
                category_id=category_id if resolved_plan is None else ...,
                plan_id=resolved_plan,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={quote(str(e))}#user-service-addons", status_code=303
            )
        return RedirectResponse(
            f"/plans?ok={quote(packs_saved_message(len(packs)))}#user-service-addons",
            status_code=303,
        )

    @app.post("/plans/addons/{pack_id}/toggle")
    async def plans_addon_toggle(
        pack_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.service_addons import get_owned_pack

        pack = await get_owned_pack(session, pack_id, staff)
        if not pack:
            return RedirectResponse(
                f"/plans?err={quote('بسته یافت نشد')}#user-service-addons",
                status_code=303,
            )
        pack.is_active = not pack.is_active
        await session.commit()
        return RedirectResponse("/plans#user-service-addons", status_code=303)

    @app.post("/plans/addons/{pack_id}/delete")
    async def plans_addon_delete(
        pack_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.service_addons import delete_pack

        try:
            await delete_pack(session, staff, pack_id)
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={quote(str(e))}#user-service-addons", status_code=303
            )
        return RedirectResponse(
            f"/plans?ok={quote('بسته حذف شد')}#user-service-addons",
            status_code=303,
        )

    @app.post("/plans/addons/{pack_id}/edit")
    async def plans_addon_edit(
        pack_id: int,
        name: str = Form(...),
        kind: str = Form(...),
        amount: float = Form(...),
        price: int = Form(...),
        description: str = Form(""),
        sort_order: int = Form(0),
        category_id: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.service_addons import update_pack

        try:
            await update_pack(
                session,
                staff,
                pack_id,
                name=name,
                kind=kind,
                amount=amount,
                price=price,
                description=description,
                sort_order=sort_order,
                category_id=category_id,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={quote(str(e))}#user-service-addons", status_code=303
            )
        return RedirectResponse(
            f"/plans?ok={quote('بسته به‌روزرسانی شد')}#user-service-addons",
            status_code=303,
        )
