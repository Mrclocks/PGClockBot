"""Plan categories + customer service-addon packs (web panel).

Both resources are shop-scoped via ``require_catalog_owner_id`` / owned getters —
platform Owner writes ``owner_reseller_id IS NULL``; resellers write only their shop.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.button_styles import parse_plan_button_style_form
from app.services.numbers import to_fa_digits
from app.services.service_addons import PackRow
from app.services.shop_scope import ShopScopeError



def pack_rows_from_form(amounts: list[float], prices: list[int], styles: list[str]) -> list[PackRow]:
    """Pair the repeated amount/price/colour fields of the pack form into rows."""
    if len(prices) != len(amounts) or len(styles) not in (0, len(amounts)):
        raise ValueError("ردیف‌های بسته کامل نیست")
    colours = styles or ["inherit"] * len(amounts)
    return [PackRow(amount=a, price=p, button_style=c) for a, p, c in zip(amounts, prices, colours, strict=True)]


# Where a category action returns: the grouped plans list, or the categories modal it came from.
CATEGORY_BOARD_URL = "/plans"
CATEGORY_MODAL_URL = "/plans?categories=1"
CATEGORY_BOARD_ANCHOR = "#plan-category-board"


def category_redirect(
    back: str, *, ok: str | None = None, err: str | None = None, category_id: int | None = None
) -> RedirectResponse:
    """Redirect after a category action. ``back`` is matched against a fixed value, never used as a URL.

    Back on the list, the page lands on the category's own group when there is one (plan-categories.js
    highlights it), else on the list.
    """
    to_board = str(back or "").strip() == "list"
    url = CATEGORY_BOARD_URL if to_board else CATEGORY_MODAL_URL
    params = [f"{key}={quote(value)}" for key, value in (("ok", ok), ("err", err)) if value]
    if params:
        url += ("&" if "?" in url else "?") + "&".join(params)
    if to_board:
        url += f"#plan-cat-{int(category_id)}" if category_id is not None and not err else CATEGORY_BOARD_ANCHOR
    return RedirectResponse(url, status_code=303)


def category_style_from_form(button_style: str) -> str | None:
    """None inherits the «ثابت» colour; "" is Telegram's plain white; anything else a named style."""
    if str(button_style).strip() == "":
        return ""
    return parse_plan_button_style_form({"button_style": button_style}, field="button_style")


def packs_saved_message(count: int) -> str:
    if count == 1:
        return "بسته افزونه ذخیره شد"
    return f"{to_fa_digits(str(count))} بسته افزونه ذخیره شد"

def register_plan_catalog_extras(app, *, require_perm, get_db):
    # —— Plan categories ——

    @app.post("/plans/categories")
    async def plans_category_create(
        name: str = Form(...),
        description: str = Form(""),
        audience: str = Form("users"),
        button_style: str = Form("inherit"),
        back: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plan_categories import create_category

        try:
            category = await create_category(
                session,
                staff,
                name=name,
                description=description,
                audience=audience,
                button_style=category_style_from_form(button_style),
            )
        except (ShopScopeError, ValueError) as e:
            return category_redirect(back, err=str(e))
        return category_redirect(back, ok="برچسب دسته ذخیره شد", category_id=int(category.id))

    @app.post("/plans/categories/{category_id}/toggle")
    async def plans_category_toggle(
        category_id: int,
        back: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plan_categories import get_owned_category

        cat = await get_owned_category(session, category_id, staff)
        if not cat:
            return category_redirect(back, err="برچسب دسته یافت نشد")
        cat.is_active = not cat.is_active
        await session.commit()
        return category_redirect(back, ok="برچسب دسته روشن شد" if cat.is_active else "برچسب دسته خاموش شد")

    @app.post("/plans/categories/{category_id}/move")
    async def plans_category_move(
        category_id: int,
        direction: str = Form(...),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plan_categories import move_category

        try:
            moved = await move_category(session, staff, category_id, direction=direction)
        except (ShopScopeError, ValueError) as e:
            return category_redirect("list", err=str(e))
        return category_redirect("list", ok="ترتیب دسته‌ها در ربات عوض شد" if moved else None)

    @app.post("/plans/categories/quick")
    async def plans_category_quick_create(
        name: str = Form(""),
        button_style: str = Form("inherit"),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        """Add a user category from inside the plan form; the same name twice returns the first one.

        The answer carries the tone of the category's bot button, so the new chip shows its colour at once.
        """
        from app.services.plan_categories import find_or_create_user_category
        from app.services.plan_category_board import category_tone
        from app.services.users import get_all_settings

        try:
            category, created = await find_or_create_user_category(
                session, staff, name=name, button_style=category_style_from_form(button_style)
            )
        except (ShopScopeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        ui = await get_all_settings(session, reseller_id=category.owner_reseller_id)
        return JSONResponse({
            "ok": True, "id": int(category.id), "name": category.name, "created": created,
            "tone": category_tone(ui, category),
        })

    @app.post("/plans/categories/{category_id}/delete")
    async def plans_category_delete(
        category_id: int,
        back: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plan_categories import delete_category

        try:
            await delete_category(session, staff, category_id)
        except (ShopScopeError, ValueError) as e:
            return category_redirect(back, err=str(e))
        return category_redirect(back, ok="برچسب دسته حذف شد")

    @app.post("/plans/categories/{category_id}/edit")
    async def plans_category_edit(
        category_id: int,
        name: str = Form(...),
        description: str = Form(""),
        audience: str = Form("users"),
        button_style: str = Form("inherit"),
        back: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        # The order is not part of this form: it changes only with «بالاتر / پایین‌تر در ربات».
        from app.services.plan_categories import update_category

        try:
            await update_category(
                session,
                staff,
                category_id,
                name=name,
                description=description,
                audience=audience,
                button_style=category_style_from_form(button_style),
            )
        except (ShopScopeError, ValueError) as e:
            return category_redirect(back, err=str(e))
        return category_redirect(back, ok="برچسب دسته به‌روزرسانی شد", category_id=category_id)

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
