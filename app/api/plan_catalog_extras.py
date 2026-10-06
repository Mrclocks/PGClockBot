"""Plan categories + customer service-addon packs (web panel).

Both resources are shop-scoped via ``require_catalog_owner_id`` / owned getters —
platform Owner writes ``owner_reseller_id IS NULL``; resellers write only their shop.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.shop_scope import ShopScopeError


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
        name: str = Form(...),
        kind: str = Form(...),
        amount: float = Form(...),
        price: int = Form(...),
        description: str = Form(""),
        sort_order: int = Form(0),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.service_addons import create_pack

        try:
            await create_pack(
                session,
                staff,
                name=name,
                kind=kind,
                amount=amount,
                price=price,
                description=description,
                sort_order=sort_order,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={quote(str(e))}#user-service-addons", status_code=303
            )
        return RedirectResponse(
            f"/plans?ok={quote('بسته افزونه ذخیره شد')}#user-service-addons",
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
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={quote(str(e))}#user-service-addons", status_code=303
            )
        return RedirectResponse(
            f"/plans?ok={quote('بسته به‌روزرسانی شد')}#user-service-addons",
            status_code=303,
        )
