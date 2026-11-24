"""The «ریست کاربران تست» tool page: one row per group with its count, and a reset behind a confirm dialog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.trial_users_pages import RETRIAL_PERMISSION, trial_users_scope
from app.services.db_safe import recover_session
from app.services.tools_nav import RETRIAL_HREF
from app.services.trial_reset import (
    RetrialGroupCount,
    parse_confirmed,
    parse_group,
    reset_trial_group,
    retrial_group_counts,
)
from app.services.web_numbers import WEB_NUMBER_FILTERS

fa_num = WEB_NUMBER_FILTERS["num"]

GROUP_ICONS = {"all": "users", "buyers": "check", "non_buyers": "time"}
GROUP_HINTS = {
    "all": "همهٔ کسانی که از این فروشگاه تست گرفته‌اند",
    "buyers": "تست گرفته‌اند و بعد خرید کرده‌اند",
    "non_buyers": "تست گرفته‌اند و هنوز خریدی نکرده‌اند",
}
# Fixed messages for the redirect; the query carries a code or a number, never text to show.
ERRORS = {
    "invalid": "درخواست نامعتبر بود؛ صفحه را تازه کنید و دوباره امتحان کنید",
    "none": "کسی از این گروه برای ریست نمانده بود",
}


@dataclass(frozen=True, slots=True)
class RetrialRow:
    """One row of the page in the shape ``status_hub`` reads."""

    key: str
    label: str
    icon: str
    summary: str
    action: str = ""
    confirm: str = ""
    confirm_title: str = "ریست کاربران تست"
    confirm_label: str = "ریست شود"
    fields: dict[str, str] = field(default_factory=dict)
    href: str = ""
    tone: str = ""
    tone_label: str = ""
    badge_class: str = ""


@dataclass(frozen=True, slots=True)
class RetrialGroupView:
    label: str
    items: tuple[RetrialRow, ...]


def retrial_row(group: RetrialGroupCount) -> RetrialRow:
    taken = f"از {fa_num(group.trials)} تست‌گرفته"
    if not group.ready:
        return RetrialRow(group.key, group.label, GROUP_ICONS[group.key], f"کسی برای ریست نیست · {taken}")
    ready = fa_num(group.ready)
    return RetrialRow(
        group.key,
        group.label,
        GROUP_ICONS[group.key],
        f"{ready} نفر آمادهٔ تست دوباره · {taken}",
        action=f"{RETRIAL_HREF}/{group.key}",
        confirm=(
            f"{ready} کاربر گروه «{group.label}» ({GROUP_HINTS[group.key]}) دوباره می‌توانند از این فروشگاه تست بگیرند."
            " سفارش‌ها و سرویس‌های قبلی‌شان دست نمی‌خورد و کاربران مسدود یا با تست فعال ریست نمی‌شوند."
        ),
        fields={"confirmed": str(group.ready)},
    )


def _flash(params) -> tuple[str | None, str | None]:
    done = params.get("reset", "")
    ok = f"{fa_num(int(done))} کاربر می‌توانند دوباره تست بگیرند" if done.isascii() and done.isdigit() and len(done) <= 10 else None
    return ok, ERRORS.get(params.get("err", ""))


def register_trial_reset_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable
) -> None:
    @app.get(RETRIAL_HREF, response_class=HTMLResponse)
    async def trial_reset_page(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> HTMLResponse:
        trial_users_scope(staff)
        shop_id = trial_users_scope(staff, RETRIAL_PERMISSION)
        await recover_session(session)
        counts = await retrial_group_counts(session, shop_id=shop_id)
        flash_ok, flash_err = _flash(request.query_params)
        groups = (RetrialGroupView("گروه کاربران", tuple(retrial_row(group) for group in counts)),)
        return render(
            request,
            "trial_reset.html",
            {"staff": staff, "groups": groups, "flash_ok": flash_ok, "flash_err": flash_err},
        )

    @app.post(RETRIAL_HREF + "/{group}")
    async def trial_reset_run(
        group: str, request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db)
    ) -> RedirectResponse:
        trial_users_scope(staff)
        shop_id = trial_users_scope(staff, RETRIAL_PERMISSION)
        form = await request.form()
        try:
            chosen = parse_group(group)
            confirmed = parse_confirmed(form.get("confirmed"))
        except ValueError:
            return RedirectResponse(f"{RETRIAL_HREF}?err=invalid", status_code=303)
        reset = await reset_trial_group(session, staff=staff, shop_id=shop_id, group=chosen, confirmed=confirmed)
        if not reset:
            return RedirectResponse(f"{RETRIAL_HREF}?err=none", status_code=303)
        return RedirectResponse(f"{RETRIAL_HREF}?reset={reset}", status_code=303)
