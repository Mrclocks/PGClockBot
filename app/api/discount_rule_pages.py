"""Marketing › «تخفیف و کد هدیه» › «تخفیف خودکار»: a shop's automatic discount rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.discount_rule_admin import (
    MAX_COUNT,
    MAX_DAYS,
    MAX_MONEY,
    MAX_PRIORITY,
    RuleInput,
    delete_rule,
    discount_rule_scope,
    list_rules,
    rule_view,
    save_rule,
    set_rule_enabled,
)
from app.services.coupon_programs import (
    MAX_MONTHLY_CAP,
    MAX_PERCENT as MAX_COUPON_PERCENT,
    MAX_VALID_DAYS,
    TRIGGERS,
    ProgramInput,
    load_programs,
    save_program,
)
from app.services.discount_rules import AUDIENCES, EVENTS, MAX_REDUCTION_BPS
from app.services.gift_codes import gift_code_plan_options
from app.services.numbers import parse_int

PAGE = "/discount-rules"


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
    if not 0 <= bps <= MAX_REDUCTION_BPS:
        raise ValueError(f"«درصد» باید بین ۰ تا {MAX_REDUCTION_BPS // 100} باشد")
    return int(bps)


def _ends_at(form: Mapping[str, Any], *, now: datetime) -> datetime | None:
    days = _int(form, "valid_days", label="مدت اعتبار", upper=MAX_DAYS, optional=True)
    return None if not days else now + timedelta(days=days)


def _plan_ids(form: Any) -> str | None:
    chosen = sorted({str(value).strip() for value in form.getlist("plan_ids") if str(value).strip()})
    return ",".join(chosen) or None


def rule_from_form(form: Any, *, now: datetime) -> RuleInput:
    """The submitted rule; raises ``ValueError`` (safe to show)."""
    mode = "fixed" if form.get("mode") == "fixed" else "percent"
    audience = str(form.get("audience") or "all")
    return RuleInput(
        name=str(form.get("name") or "").strip()[:128],
        audience=audience if audience in AUDIENCES else "",
        min_purchases=int(_int(form, "min_purchases", label="تعداد خرید قبلی", upper=MAX_COUNT) or 0),
        audience_days=int(_int(form, "audience_days", label="تعداد روز", upper=MAX_DAYS) or 0),
        events=",".join(event for event in EVENTS if form.get(f"event_{event}")),
        plan_ids=_plan_ids(form),
        mode=mode,
        percent_bps=_percent_bps(form) if mode == "percent" else 0,
        fixed_amount=int(_int(form, "fixed_amount", label="مبلغ ثابت") or 0) if mode == "fixed" else 0,
        max_per_order=_int(form, "max_per_order", label="سقف هر سفارش", optional=True),
        min_order_amount=int(_int(form, "min_order_amount", label="حداقل خرید") or 0),
        customer_max_count=_int(form, "customer_max_count", label="سقف هر مشتری", upper=MAX_COUNT, optional=True),
        max_uses=_int(form, "max_uses", label="سقف کل استفاده", upper=MAX_COUNT, optional=True),
        ends_at=_ends_at(form, now=now),
        priority=int(_int(form, "priority", label="اولویت", upper=MAX_PRIORITY) or 0),
    )


def program_from_form(form: Any) -> ProgramInput:
    """The submitted coupon program; raises ``ValueError`` (safe to show)."""
    return ProgramInput(
        enabled=bool(form.get("enabled")),
        percent=int(_int(form, "percent", label="درصد", upper=MAX_COUPON_PERCENT) or 0),
        max_discount=_int(form, "max_discount", label="سقف تخفیف", optional=True) or None,
        min_purchase=int(_int(form, "min_purchase", label="حداقل خرید") or 0),
        valid_days=int(_int(form, "valid_days", label="مدت اعتبار", upper=MAX_VALID_DAYS) or 0),
        monthly_cap=_int(form, "monthly_cap", label="سقف ماهانه", upper=MAX_MONTHLY_CAP, optional=True) or None,
    )


def register_discount_rule_pages(app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable) -> None:
    @app.get(PAGE, response_class=HTMLResponse)
    async def discount_rules_page(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> Any:
        try:
            shop_id = discount_rule_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        rules, now = await list_rules(session, shop_id), datetime.now(timezone.utc)
        return render(request, "discount_rules.html", {
            "staff": staff, "rules": rules,
            "plans": await gift_code_plan_options(session, shop_id=shop_id),
            "views": {rule.id: rule_view(rule, now=now) for rule in rules},
            "programs": await load_programs(session, shop_id), "triggers": TRIGGERS,
            "flash_ok": request.query_params.get("ok"), "flash_err": request.query_params.get("err"),
        })

    async def form_action(request: Request, staff: dict, action: Callable) -> RedirectResponse:
        try:
            shop_id = discount_rule_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            message = await action(shop_id, form)
        except ValueError as exc:
            return _back(err=str(exc))
        return _back(ok=message)

    _register_rule_routes(app, form_action=form_action, require_staff=require_staff, get_db=get_db)


def _register_rule_routes(app: FastAPI, *, form_action: Callable, require_staff: Callable, get_db: Callable) -> None:
    @app.post(f"{PAGE}/rules")
    async def discount_rule_create(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Any) -> str:
            await save_rule(session, shop_id, rule_from_form(form, now=datetime.now(timezone.utc)))
            return "قانون تخفیف ساخته شد"
        return await form_action(request, staff, action)

    @app.post(PAGE + "/rules/{rule_id}")
    async def discount_rule_edit(
        rule_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Any) -> str:
            await save_rule(session, shop_id, rule_from_form(form, now=datetime.now(timezone.utc)), rule_id=rule_id)
            return "قانون تخفیف ذخیره شد"
        return await form_action(request, staff, action)

    @app.post(PAGE + "/rules/{rule_id}/toggle")
    async def discount_rule_toggle(
        rule_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Any) -> str:
            await set_rule_enabled(session, shop_id, rule_id, bool(form.get("enabled")))
            return "وضعیت قانون ذخیره شد"
        return await form_action(request, staff, action)

    @app.post(PAGE + "/coupons/{trigger}")
    async def coupon_program_save(
        trigger: str, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Any) -> str:
            await save_program(session, shop_id, trigger, program_from_form(form))
            return "کوپن خودکار ذخیره شد"
        return await form_action(request, staff, action)

    @app.post(PAGE + "/rules/{rule_id}/delete")
    async def discount_rule_delete(
        rule_id: int, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        async def action(shop_id: int | None, form: Any) -> str:
            await delete_rule(session, shop_id, rule_id)
            return "قانون تخفیف حذف شد"
        return await form_action(request, staff, action)
