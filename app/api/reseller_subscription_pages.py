"""«اشتراک من»: a shop owner's own package in the web panel (renew, move package, extras).

Thin routes over ``app.services.reseller_package`` and ``app.services.reseller_capacity``,
the same rules the bot uses. Only the shop owner's session (role ``reseller``) reaches
it; the shop comes from that session, never from the form. Everything is paid from the
owner's wallet.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, ResellerPlan, ResellerProfile
from app.services.db_safe import rollback_quiet
from app.services.redact import user_safe_error
from app.services.reseller_capacity import (
    ALLOWED_EXTRA_GB,
    ALLOWED_EXTRA_USERS,
    buy_extra_gb,
    buy_extra_users,
    plan_allows_buy_extra,
    plan_extra_gb_price,
    plan_extra_user_price,
)
from app.services.reseller_package import package_overview, renew_package

logger = logging.getLogger(__name__)

PAGE = "/my-subscription"
_ID_MAX = 2**31 - 1


def _back(*, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"ok={quote(ok)}" if ok else f"err={quote(err)}"
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


async def _owner_context(session: AsyncSession, staff: dict) -> tuple[BotUser, ResellerProfile]:
    """The shop owner's account and profile; staff sessions and other roles get a 403."""
    if staff.get("role") != "reseller" or not staff.get("bot_user_id"):
        raise HTTPException(403, "این بخش فقط برای مالک فروشگاه است")
    owner_id = int(staff["bot_user_id"])
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == owner_id))
    owner = await session.get(BotUser, owner_id)
    if profile is None or owner is None:
        raise HTTPException(403, "این بخش فقط برای مالک فروشگاه است")
    return owner, profile


def _valid_id(value: int) -> int:
    if not 0 < int(value) <= _ID_MAX:
        raise ValueError("بسته نامعتبر است")
    return int(value)


def register_reseller_subscription_pages(app, *, render, require_staff, get_db) -> None:
    @app.get(PAGE, response_class=HTMLResponse)
    async def my_subscription(
        request: Request, staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ):
        owner, profile = await _owner_context(session, staff)
        ctx: dict = {
            "staff": staff,
            "wallet": int(owner.wallet_balance or 0),
            "overview": None,
            "unavailable": "",
            "extras": None,
            "bot_username": (get_settings().bot_username or "").lstrip("@"),
            "flash_ok": request.query_params.get("ok"),
            "flash_err": request.query_params.get("err"),
        }
        try:
            overview = await package_overview(session, profile)
        except ValueError as exc:
            ctx["unavailable"] = user_safe_error(exc, fallback="اشتراک این فروشگاه در دسترس نیست")
            return render(request, "my_subscription.html", ctx)
        ctx["overview"] = overview
        current = overview.current
        if plan_allows_buy_extra(current):
            ctx["extras"] = {
                "gb_price": plan_extra_gb_price(current), "user_price": plan_extra_user_price(current),
                "gb_options": sorted(ALLOWED_EXTRA_GB), "user_options": sorted(ALLOWED_EXTRA_USERS),
            }
        return render(request, "my_subscription.html", ctx)

    @app.post(f"{PAGE}/renew")
    async def my_subscription_renew(
        plan_id: int = Form(...),
        generation: int = Form(...),
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        owner, profile = await _owner_context(session, staff)
        try:
            result = await renew_package(
                session, owner=owner, profile=profile, plan_id=_valid_id(plan_id), generation=int(generation),
            )
        except ValueError as exc:
            await rollback_quiet(session)
            return _back(err=user_safe_error(exc, fallback="تمدید ناموفق بود"))
        moved = f" — بستهٔ جدید: {result['plan_name']}" if result.get("changed") else ""
        return _back(ok=f"تمدید انجام شد{moved}")

    @app.post(f"{PAGE}/extra")
    async def my_subscription_extra(
        kind: str = Form(...),
        amount: int = Form(...),
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        owner, profile = await _owner_context(session, staff)
        current = await session.get(ResellerPlan, int(profile.plan_id)) if profile.plan_id else None
        if kind not in {"gb", "users"} or current is None:
            return _back(err="درخواست نامعتبر است")
        try:
            if kind == "gb":
                await buy_extra_gb(session, user=owner, profile=profile, plan=current, gb=int(amount))
            else:
                await buy_extra_users(session, user=owner, profile=profile, plan=current, count=int(amount))
        except ValueError as exc:
            await rollback_quiet(session)
            return _back(err=user_safe_error(exc, fallback="خرید ناموفق بود"))
        return _back(ok="ظرفیت اضافه شد")
