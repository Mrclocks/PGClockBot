"""Marketing › «تخفیف و کد هدیه» › «کش‌بک»: a shop's cashback rules and settings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.cashback_rules import (
    COMBINE_MODES,
    EVENTS,
    MAX_MONEY,
    CashbackSettings,
    RuleInput,
    budget_used,
    cashback_scope,
    delete_rule,
    list_rules,
    load_settings,
    save_rule,
    save_settings,
    set_rule_enabled,
)
from app.services.numbers import parse_int

PAGE = "/cashback"
MAX_PERCENT_BPS = 10_000
MAX_HOLD_HOURS = 24 * 90


def _back(*, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"err={quote(err)}" if err else f"ok={quote(ok)}"
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


def _int(form: Mapping[str, Any], key: str, *, label: str, upper: int = MAX_MONEY, optional: bool = False) -> int | None:
    raw = str(form.get(key) or "").strip()
    if not raw:
        return None if optional else 0
    try:
        value = parse_int(raw)
    except ValueError:
        raise ValueError(f"«{label}» باید عدد باشد") from None
    if value < 0 or value > upper:
        raise ValueError(f"«{label}» خارج از محدودهٔ مجاز است")
    return value


def _percent_bps(form: Mapping[str, Any]) -> int:
    """Percent typed with up to two decimals («2.5») → basis points."""
    raw = str(form.get("percent") or "").strip().replace("٫", ".")
    if not raw:
        return 0
    try:
        bps = round(float(raw) * 100)
    except ValueError:
        raise ValueError("«درصد» باید عدد باشد") from None
    if not 0 <= bps <= MAX_PERCENT_BPS:
        raise ValueError("«درصد» باید بین ۰ تا ۱۰۰ باشد")
    return int(bps)


def rule_from_form(form: Mapping[str, Any]) -> RuleInput:
    """The submitted rule; raises ``ValueError`` (safe to show)."""
    name = str(form.get("name") or "").strip()[:128]
    if not name:
        raise ValueError("نام قانون الزامی است")
    mode = "fixed" if form.get("mode") == "fixed" else "percent"
    events = [e for e in EVENTS if form.get(f"event_{e}")]
    if not events:
        raise ValueError("حداقل یک نوع خرید را انتخاب کنید")
    data = RuleInput(
        name=name, mode=mode,
        percent_bps=_percent_bps(form) if mode == "percent" else 0,
        fixed_amount=int(_int(form, "fixed_amount", label="مبلغ ثابت") or 0) if mode == "fixed" else 0,
        max_per_order=_int(form, "max_per_order", label="سقف هر سفارش", optional=True),
        min_order_amount=int(_int(form, "min_order_amount", label="حداقل خرید") or 0),
        events=",".join(events),
        first_purchase_only=bool(form.get("first_purchase_only")),
        allow_with_discount=bool(form.get("allow_with_discount")),
        customer_max_count=_int(form, "customer_max_count", label="تعداد در ماه", upper=10_000, optional=True),
        customer_max_amount=_int(form, "customer_max_amount", label="مبلغ در ماه", optional=True),
        priority=int(_int(form, "priority", label="اولویت", upper=1_000) or 0),
    )
    if data.percent_bps == 0 and data.fixed_amount == 0:
        raise ValueError("درصد یا مبلغ کش‌بک باید بیشتر از صفر باشد")
    return data


def settings_from_form(form: Mapping[str, Any]) -> CashbackSettings:
    combine = str(form.get("combine") or "best")
    return CashbackSettings(
        enabled=bool(form.get("enabled")),
        combine=combine if combine in COMBINE_MODES else "best",
        hold_hours=int(_int(form, "hold_hours", label="دورهٔ انتظار", upper=MAX_HOLD_HOURS) or 0),
        monthly_budget=int(_int(form, "monthly_budget", label="بودجهٔ ماهانه") or 0),
    )


def register_cashback_pages(app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable) -> None:
    @app.get(PAGE, response_class=HTMLResponse)
    async def cashback_page(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> Any:
        try:
            shop_id = cashback_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        return render(request, "cashback.html", {
            "staff": staff, "rules": await list_rules(session, shop_id),
            "settings": await load_settings(session, shop_id), "budget_used": await budget_used(session, shop_id),
            "flash_ok": request.query_params.get("ok"), "flash_err": request.query_params.get("err"),
        })

    async def _form_action(request: Request, staff: dict, action: Callable) -> RedirectResponse:
        try:
            shop_id = cashback_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            message = await action(shop_id, form)
        except ValueError as exc:
            return _back(err=str(exc))
        return _back(ok=message)

    @app.post(f"{PAGE}/settings")
    async def cashback_settings(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Mapping[str, Any]) -> str:
            await save_settings(session, shop_id, settings_from_form(form))
            return "تنظیمات کش‌بک ذخیره شد"
        return await _form_action(request, staff, action)

    _register_rule_routes(app, form_action=_form_action, require_staff=require_staff, get_db=get_db)


def _register_rule_routes(app: FastAPI, *, form_action: Callable, require_staff: Callable, get_db: Callable) -> None:
    @app.post(f"{PAGE}/rules")
    async def cashback_rule_create(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Mapping[str, Any]) -> str:
            await save_rule(session, shop_id, rule_from_form(form))
            return "قانون کش‌بک ساخته شد"
        return await form_action(request, staff, action)

    @app.post(PAGE + "/rules/{rule_id}")
    async def cashback_rule_edit(
        rule_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Mapping[str, Any]) -> str:
            await save_rule(session, shop_id, rule_from_form(form), rule_id=rule_id)
            return "قانون کش‌بک ذخیره شد"
        return await form_action(request, staff, action)

    @app.post(PAGE + "/rules/{rule_id}/toggle")
    async def cashback_rule_toggle(
        rule_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Mapping[str, Any]) -> str:
            await set_rule_enabled(session, shop_id, rule_id, bool(form.get("enabled")))
            return "وضعیت قانون ذخیره شد"
        return await form_action(request, staff, action)

    @app.post(PAGE + "/rules/{rule_id}/delete")
    async def cashback_rule_delete(
        rule_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Mapping[str, Any]) -> str:
            await delete_rule(session, shop_id, rule_id)
            return "قانون کش‌بک حذف شد"
        return await form_action(request, staff, action)
