"""«سوالات متداول» page of the bot settings: list, add, edit, order, show or hide, delete, import.

The shop is resolved from the signed-in staff only: the platform owner edits the platform bot's
questions, a shop's staff with ``shop_settings`` edit their own shop's, and anyone else is refused.
No form field names a shop. Rules and validation live in ``app.services.faq``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.button_styles import PLAN_BUTTON_STYLE_OPTIONS
from app.services.faq import (
    INHERIT_STYLE,
    MAX_FAQ_ITEMS,
    NOT_FOUND,
    FaqDraft,
    FaqError,
    create_faq_item,
    delete_faq_item,
    draft_of,
    get_faq_item,
    import_legacy_faq,
    list_faq_items,
    move_faq_item,
    update_faq_item,
)
from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.rich_text import rich_plain_text
from app.services.settings_tab_groups import (
    CUSTOMER_AREA,
    FAQ_PAGE,
    FAQ_TAB_KEY,
    group_settings_tabs,
    settings_area,
    tabs_with_page_tabs,
)
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.users import SETTINGS_TABS, get_all_settings

logger = logging.getLogger(__name__)

PERMISSION = "shop_settings"
NEW_KEY = "new"
# Fixed result messages; the address carries only the code, never text to show.
RESULTS = {
    "created": "سوال اضافه شد.",
    "saved": "سوال ذخیره شد.",
    "moved": "ترتیب سوال‌ها عوض شد.",
    "shown": "سوال برای مشتری نمایش داده می‌شود.",
    "hidden": "سوال از دید مشتری پنهان شد.",
    "deleted": "سوال حذف شد.",
    "imported": "متن قبلی به سوال‌ها تبدیل شد.",
}
NO_SHOP = "محدوده فروشگاه مشخص نیست — به این بخش دسترسی ندارید"


@dataclass(frozen=True, slots=True)
class FaqScope:
    """The shop a staff member edits (``None`` = platform) and where its settings live."""

    reseller_id: int | None
    settings_base: str


def faq_scope(staff: dict) -> FaqScope | None:
    """Platform for the owner, the staff's own shop for shop staff, ``None`` for anyone else."""
    if is_platform_admin(staff):
        return FaqScope(None, "/settings")
    shop = shop_owner_id(staff)
    return FaqScope(shop, "/shop-settings") if shop else None


def draft_from_form(form: dict) -> FaqDraft:
    """The submitted fields as a draft; ``FaqError`` from the service checks the values."""
    # "" is the white client default, so only a missing field means «inherit».
    style = str(form.get("button_style", INHERIT_STYLE))
    return FaqDraft(
        question=str(form.get("question") or ""),
        answer=str(form.get("answer") or ""),
        button_style=None if style == INHERIT_STYLE else style,
        icon_custom_emoji_id=str(form.get("icon_custom_emoji_id") or "") or None,
        is_active=str(form.get("is_active") or "") in {"1", "on", "true"},
    )


def parse_item_id(raw: object) -> int | None:
    text = str(raw or "")
    return int(text) if text.isdigit() and len(text) <= 10 else None


def _done(code: str) -> RedirectResponse:
    return RedirectResponse(f"{FAQ_PAGE}?ok={code}", status_code=303)


def _deny() -> RedirectResponse:
    return RedirectResponse("/dashboard?err=" + quote(NO_SHOP), status_code=303)


def register_faq_pages(  # noqa: C901 - counts the nested routes; each is listed on its own
    app: object,
    *,
    render: Callable[..., HTMLResponse],
    require_perm: Callable[[str], Callable[..., object]],
    get_db: Callable[..., object],
) -> None:
    require_shop = require_perm(PERMISSION)

    async def page(
        request: Request,
        session: AsyncSession,
        staff: dict,
        scope: FaqScope,
        *,
        error: str | None = None,
        reopen: tuple[str, FaqDraft] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        items = await list_faq_items(session, reseller_id=scope.reseller_id)
        legacy = ""
        if not items:
            legacy = rich_plain_text((await get_all_settings(session, reseller_id=scope.reseller_id)).get("faq_text"))
        tabs = SETTINGS_TABS if scope.reseller_id is None else RESELLER_SETTINGS_TABS
        ok = RESULTS.get(request.query_params.get("ok") or "")
        context = {
            "staff": staff,
            "items": items,
            "drafts": {item.id: draft_of(item) for item in items},
            "max_items": MAX_FAQ_ITEMS,
            "can_add": len(items) < MAX_FAQ_ITEMS,
            "has_legacy": bool(legacy.strip()),
            "total_views": sum(item.view_count for item in items),
            "total_tickets": sum(item.ticket_count for item in items),
            "style_options": PLAN_BUTTON_STYLE_OPTIONS,
            "tab_nav": group_settings_tabs(tabs_with_page_tabs(tabs, staff), FAQ_TAB_KEY, area=CUSTOMER_AREA),
            "settings_area": settings_area(CUSTOMER_AREA, base=scope.settings_base, bot_title="تنظیمات ربات"),
            "settings_base": scope.settings_base,
            "reopen_key": reopen[0] if reopen else None,
            "reopen_draft": reopen[1] if reopen else None,
            "flash_ok": None if error else ok,
            "flash_err": error,
        }
        return render(request, "faq_settings.html", context, status_code=status_code)

    async def refused(
        request: Request, session: AsyncSession, staff: dict, scope: FaqScope, exc: FaqError, key: str, draft: FaqDraft
    ) -> HTMLResponse:
        """The page again with the reason and the form reopened with what was typed."""
        return await page(request, session, staff, scope, error=str(exc), reopen=(key, draft), status_code=400)

    @app.get(FAQ_PAGE, response_class=HTMLResponse)
    async def faq_page(request: Request, staff: dict = Depends(require_shop), session: AsyncSession = Depends(get_db)):
        scope = faq_scope(staff)
        if scope is None:
            return _deny()
        return await page(request, session, staff, scope)

    @app.post(FAQ_PAGE)
    async def faq_create(request: Request, staff: dict = Depends(require_shop), session: AsyncSession = Depends(get_db)):
        scope = faq_scope(staff)
        if scope is None:
            return _deny()
        draft = draft_from_form(dict(await request.form()))
        try:
            await create_faq_item(session, draft, reseller_id=scope.reseller_id)
        except FaqError as exc:
            return await refused(request, session, staff, scope, exc, NEW_KEY, draft)
        return _done("created")

    @app.post(FAQ_PAGE + "/import")
    async def faq_import(request: Request, staff: dict = Depends(require_shop), session: AsyncSession = Depends(get_db)):
        scope = faq_scope(staff)
        if scope is None:
            return _deny()
        raw = (await get_all_settings(session, reseller_id=scope.reseller_id)).get("faq_text")
        try:
            await import_legacy_faq(session, raw, reseller_id=scope.reseller_id)
        except FaqError as exc:
            return await page(request, session, staff, scope, error=str(exc), status_code=400)
        return _done("imported")

    @app.post(FAQ_PAGE + "/{item_id}")
    async def faq_update(
        item_id: str, request: Request, staff: dict = Depends(require_shop), session: AsyncSession = Depends(get_db)
    ):
        scope, iid = faq_scope(staff), parse_item_id(item_id)
        if scope is None:
            return _deny()
        draft = draft_from_form(dict(await request.form()))
        try:
            if iid is None:
                raise FaqError(NOT_FOUND)
            await update_faq_item(session, iid, draft, reseller_id=scope.reseller_id)
        except FaqError as exc:
            return await refused(request, session, staff, scope, exc, str(iid or ""), draft)
        return _done("saved")

    @app.post(FAQ_PAGE + "/{item_id}/{action}")
    async def faq_action(
        item_id: str, action: str, request: Request,
        staff: dict = Depends(require_shop), session: AsyncSession = Depends(get_db),
    ):
        scope, iid = faq_scope(staff), parse_item_id(item_id)
        if scope is None:
            return _deny()
        if iid is None or action not in {"up", "down", "show", "hide", "delete"}:
            return await page(request, session, staff, scope, error="درخواست نامعتبر است.", status_code=400)
        try:
            code = await _apply_action(session, iid, action, scope.reseller_id)
        except FaqError as exc:
            return await page(request, session, staff, scope, error=str(exc), status_code=400)
        return _done(code)


async def _apply_action(session: AsyncSession, item_id: int, action: str, reseller_id: int | None) -> str:
    """Run one row action; returns its result code."""
    if action == "delete":
        await delete_faq_item(session, item_id, reseller_id=reseller_id)
        return "deleted"
    if action in {"up", "down"}:
        await move_faq_item(session, item_id, step=-1 if action == "up" else 1, reseller_id=reseller_id)
        return "moved"
    item = await get_faq_item(session, item_id, reseller_id=reseller_id)
    draft = draft_of(item)
    draft.is_active = action == "show"
    await update_faq_item(session, item_id, draft, reseller_id=reseller_id)
    return "shown" if draft.is_active else "hidden"
