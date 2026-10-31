"""Telegram Mini App pages + JSON API (role-aware shells).

Security invariants (must not regress):
- initData HMAC uses the **platform** bot token only (shop bots never host this app).
- Every service/QR/buy/renew/addon action is scoped to ``BotUser`` from initData — no cross-user IDs.
- Subscription info uses the service's own ``subscription_token`` with ``auth=False``
  (never Owner/reseller PG admin credentials for another tenant).
- Platform **admin/owner** gets ops overview only — **no** shop buy/renew/wallet commerce.
- Catalog in Mini App is **platform plans only** (hard shop isolation; no other reseller catalog).
- Feature off (no HTTPS / public URL) → page + APIs return 404.
- End-user force-join is enforced on every authenticated Mini App call (bot parity).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog_pricing import displayed_quote, quote_product, sale_price
from app.config import get_settings
from app.db.models import BotUser, Order, ResellerProfile, UserService
from app.services.db_safe import rollback_quiet
from app.services.funnel_events import STEP_PAY_START, record_order_funnel
from app.services.service_scope import customer_may_use_service
from app.services.trial_notify import notify_trial_claimed_outside_bot
from app.services.formatting import (
    expire_remaining_days,
    format_bytes_left,
    format_bytes_ratio,
    format_expire_short,
    hold_duration_from_info,
    is_on_hold_status,
    parse_expire,
    status_label_plain,
    time_remaining_label,
)
from app.services.miniapp_auth import (
    assert_mini_force_join,
    assert_miniapp_feature_enabled,
    load_mini_user,
    resolve_mini_persona,
)
from app.services.miniapp_branding import platform_brand, shop_brand
from app.services.miniapp_badges import home_badges
from app.services.miniapp_home import customer_home_summary
from app.services.miniapp_shop import (
    KEY_PATTERN,
    PLATFORM_SCOPE,
    SHOP_SECTIONS,
    SHOP_UNAVAILABLE,
    MiniScope,
    mini_scope,
    resolve_shop_scope,
    shop_display_name,
    shop_nav,
)
from app.services.pasarguard import get_pg
from app.services.service_live_info import fetch_subscription_info, service_info_error, validate_service_info
from app.services.users import current_shop_reseller_id, get_all_settings, on
from app.services.wallet import get_wallet_balance

log = logging.getLogger(__name__)

# Personas allowed to buy/renew/wallet commerce in Mini App (never platform admin/owner).
_COMMERCE_PERSONAS = frozenset({"user", "reseller"})


def _no_store(payload: dict) -> JSONResponse:
    resp = JSONResponse(payload)
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


def commerce_allowed(persona: str) -> bool:
    return (persona or "").strip() in _COMMERCE_PERSONAS


def _nav_for(persona: str) -> list[dict[str, str]]:
    """Nav is persona-scoped — admin never sees shop/wallet/services commerce tabs."""
    if persona == "admin":
        return [
            {"id": "home", "label": "داشبورد", "icon": "home"},
            {"id": "ops", "label": "میانبرها", "icon": "ops"},
        ]
    base = [
        {"id": "home", "label": "خانه", "icon": "home"},
        {"id": "services", "label": "سرویس‌ها", "icon": "svc"},
        {"id": "shop", "label": "خرید", "icon": "shop"},
    ]
    account = {"id": "account", "label": "حساب", "icon": "account"}
    if persona == "reseller":
        return base + [{"id": "ops", "label": "فروشگاه", "icon": "store"}, account]
    return base + [account]


def _require_commerce(user: BotUser) -> str:
    persona = resolve_mini_persona(user)
    if not commerce_allowed(persona):
        raise HTTPException(403, "خرید/تمدید برای این نقش در مینی‌اپ مجاز نیست")
    return persona


async def _require_commerce_ready(session: AsyncSession, user: BotUser) -> str:
    """Commerce persona + force-join (users) before mutating wallet/orders.

    In a shop's Mini App ``load_mini_user`` already checked that shop's own channels;
    checking the platform's here would gate shop customers on the wrong channels.
    """
    persona = _require_commerce(user)
    if current_shop_reseller_id() is None:
        await assert_mini_force_join(session, user)
    return persona


async def _scope_ui(session: AsyncSession, request: Request) -> dict:
    """Settings of this Mini App's shop (the platform's outside a shop)."""
    return await get_all_settings(session, reseller_id=mini_scope(request).shop_id)


async def _scope_wallet(session: AsyncSession, user: BotUser, request: Request) -> int:
    """The purse this Mini App pays from: the shop's in a shop, the platform's otherwise."""
    return await get_wallet_balance(session, user, shop_id=mini_scope(request).shop_id)


async def _owned_service_or_404(
    session: AsyncSession, svc: UserService | None, user: BotUser, *, shop_id: int | None = None
) -> UserService:
    """Fail closed: the service must be the Mini App user's and sold by this Mini App's shop."""
    if not await customer_may_use_service(session, svc, int(user.id), shop_id):
        raise HTTPException(404)
    return svc


def _addons_allowed(user: BotUser, svc: UserService) -> bool:
    """Packs for a platform-sold, panel-linked service (shop-sold ones never reach this check)."""
    return bool(svc.pg_user_id) and (svc.remark or "").strip() != "linked"


async def _addon_service(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None = None
) -> UserService:
    svc = await _owned_service_or_404(session, await session.get(UserService, service_id), user, shop_id=shop_id)
    if not _addons_allowed(user, svc):
        raise HTTPException(400, "خرید بسته برای این سرویس در مینی‌اپ ممکن نیست")
    return svc


def _traffic_pct(used, limit) -> int | None:
    try:
        lim = float(limit or 0)
        if lim <= 0:
            return None
        u = float(used or 0)
        return int(min(100, max(0, round(100.0 * u / lim))))
    except (TypeError, ValueError):
        return None


def _service_sub_token(svc: UserService) -> str | None:
    """Prefer stored token; fall back to extracting from subscription_url.

    Older rows (or failed extract at mint time) may have a URL but a null
    ``subscription_token`` — without this, Mini App enrichment shows blanks
    and QR/status look broken even though the service exists.
    """
    token = (svc.subscription_token or "").strip()
    if token:
        return token
    from app.services.pasarguard import extract_sub_token

    return extract_sub_token(svc.subscription_url)


async def _fetch_pg_info(subscription_token: str | None, *, subscription_url: str | None = None,
                         service_id: int | None = None, expected_pg_user_id: int | None = None) -> dict:
    """Fresh public subscription_info (auth=False); never fall back to admin JWT."""
    return await fetch_subscription_info(
        subscription_token, subscription_url=subscription_url, service_id=service_id,
        expected_pg_user_id=expected_pg_user_id, client_factory=get_pg,
    )


def _safe_client_message(exc: BaseException, *, fallback: str) -> str:
    """Never echo English/internal exception text to the Mini App client."""
    msg = str(exc or "").strip()
    if not msg or len(msg) > 180:
        return fallback
    low = msg.lower()
    # Block known internal / upstream leak patterns (incl. ASCII-only ops errors).
    blocked = (
        "traceback",
        "sqlalchemy",
        "pasarguard",
        "httpx",
        "panel user",
        "plan missing",
        "exception",
        "timeout",
        "connection",
        "stack",
        "file \"",
        "errno",
    )
    if any(b in low for b in blocked):
        return fallback
    # Prefer Persian user-facing copy; pure ASCII messages are usually internal.
    if all(ord(c) < 128 for c in msg):
        return fallback
    return msg


def _iso_time(value: object) -> str | None:
    """A panel time as ISO 8601 UTC; the Mini App formats it in the reader's calendar."""
    moment = parse_expire(value)
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def _serialize_service(svc: UserService, info: dict | None = None) -> dict:
    info = validate_service_info(info)
    # Only our sentinel may appear as ``error`` — never raw upstream strings.
    upstream_err = not info or info.get("error") == "upstream_unavailable"
    used = info.get("used_traffic")
    limit = info.get("data_limit")
    expire = info.get("expire") if "expire" in info else info.get("expire_date")
    status_raw = (info.get("status") or "").strip() or None
    hold_dur = hold_duration_from_info(info) if not upstream_err else None
    days = (
        expire_remaining_days(expire, status=status_raw, expire_duration=hold_dur)
        if not upstream_err
        else None
    )
    pending = bool(not upstream_err and is_on_hold_status(status_raw))
    # Omitted fields are unknown; explicit null/zero limits can mean unlimited.
    volume_known = not upstream_err and "data_limit" in info and "used_traffic" in info
    time_known = not upstream_err and (
        "expire" in info or "expire_date" in info or pending
    )
    # Never expose subscription_token — only the share URL the user already owns.
    from app.services.pasarguard import absolutize_subscription_url

    sub_url = absolutize_subscription_url(svc.subscription_url) or (svc.subscription_url or "")
    return {
        "id": svc.id,
        "is_cancelled": bool(getattr(svc, "is_cancelled", False)),
        "cancellation_pending": bool(getattr(svc, "cancellation_pending", False)),
        "username": svc.pg_username or "",
        "subscription_url": sub_url,
        "plan_id": svc.plan_id,
        "status": status_raw or "—",
        "status_fa": status_label_plain(status_raw)
        if status_raw
        else ("—" if not info or upstream_err else "نامشخص"),
        "traffic": format_bytes_ratio(used, limit, joiner=" از ")
        if volume_known
        else "—",
        "traffic_pct": _traffic_pct(used, limit) if volume_known else None,
        "traffic_left": format_bytes_left(used, limit) if volume_known else "—",
        "expire": format_expire_short(expire, status=status_raw, expire_duration=hold_dur)
        if time_known
        else "—",
        "expire_at": _iso_time(expire) if time_known else None,
        "expire_days": days,
        "expire_days_label": time_remaining_label(days_left=days, status=status_raw)
        if time_known
        else "—",
        "pending_start": pending,
        # A past moment, not an expiry: sent raw so it is never labelled "expired".
        "online_at": _iso_time(info.get("online_at")) if not upstream_err else None,
        "error": info.get("error"),
        "error_code": info.get("error_code"),
        "error_message": info.get("error_message"),
        "info_fetched_at": info.get("info_fetched_at") if not upstream_err else None,
    }


def _unavailable_service_row(svc: UserService) -> dict:
    """A local row remains visible even if upstream formatting fails."""
    return {
        "id": svc.id, "username": svc.pg_username or "", "plan_id": svc.plan_id,
        "is_cancelled": bool(getattr(svc, "is_cancelled", False)),
        "cancellation_pending": bool(getattr(svc, "cancellation_pending", False)),
        "subscription_url": "", "status": "—", "status_fa": "—",
        "traffic": "—", "traffic_pct": None, "traffic_left": "—", "expire": "—", "expire_at": None,
        "expire_days": None, "expire_days_label": "—", "pending_start": False,
        "online_at": None, "info_fetched_at": None,
        **service_info_error("invalid_response"),
    }


async def _enrich_services(services: list[UserService], *, session=None) -> list[dict]:
    if not services:
        return []
    infos = await asyncio.gather(*[
        _fetch_pg_info(_service_sub_token(s), subscription_url=s.subscription_url,
                       service_id=s.id, expected_pg_user_id=getattr(s, "pg_user_id", None))
        for s in services[:20]
    ], return_exceptions=True)
    out = []
    for index, svc in enumerate(services):
        info = infos[index] if index < len(infos) else service_info_error("not_loaded")
        if isinstance(info, BaseException):
            info = service_info_error()
        try:
            out.append(_serialize_service(svc, info))
            if session is not None and info.get("status") and "expire" in info:
                from app.services.bot_user_admin import sync_service_quota_cache
                sync_service_quota_cache(svc, info=info)
        except Exception:
            log.warning("miniapp serialization failed service=%s", svc.id)
            out.append(_unavailable_service_row(svc))
    if session is not None:
        await session.commit()
    return out


async def _customer_summary(session: AsyncSession, user: BotUser, *, scope: MiniScope = PLATFORM_SCOPE) -> dict:
    """Light launch payload: wallet, enabled features and the cached home summary.

    Plans, activity and live service usage load on their own screens. In a shop's Mini App
    everything is that shop's: its services, its purse and its settings, and paying stays
    off until shop purchases are opened.
    """
    ui = await get_all_settings(session, reseller_id=scope.shop_id)
    try:
        home = (await customer_home_summary(session, int(user.id), shop_id=scope.shop_id)).payload()
    except SQLAlchemyError:
        await rollback_quiet(session)
        log.warning("miniapp home summary failed user=%s", user.id, exc_info=True)
        home = _empty_home()
    return {
        "wallet": await get_wallet_balance(session, user, shop_id=scope.shop_id),
        "wallet_pay_enabled": on(ui.get("pay_wallet_enabled")),
        "qr_enabled": on(ui.get("qr_enabled", "1")),
        "commerce_allowed": True,
        "home": home,
        "badges": (await home_badges(session, user, shop_id=scope.shop_id)).payload(),
    }


async def _shop_me_payload(session: AsyncSession, user: BotUser, scope: MiniScope) -> dict:
    """``/me`` in a shop's Mini App: everyone is that shop's customer, with its open sections only."""
    customer = await _customer_summary(session, user, scope=scope)
    name = await shop_display_name(session, scope)
    brand = await shop_brand(session, shop_id=int(scope.shop_id), name=name)
    return {
        "persona": "user",
        "nav": shop_nav(),
        "sections": list(SHOP_SECTIONS),
        "shop": {"name": name},
        "brand": brand.payload(),
        "user": {"id": user.id, "name": user.full_name or "", "role": "user", "wallet": customer["wallet"]},
        "currency": get_settings().currency or "تومان",
        "panel_base": "",
        "commerce_allowed": True,
        "customer": customer,
    }


def _empty_home() -> dict:
    return {"service_count": 0, "attention": [], "recent": []}


def _mini_panel_base() -> str:
    """Canonical panel URL for Mini App open-links.

    Uses the live panel address (HTTPS domain when SSL is on, otherwise HTTP+IP).
    Never PUBLIC_BASE_URL — that is the web-panel / webhook host and can differ.
    """
    try:
        from app.services.ssl_certs import public_panel_base_url

        return (public_panel_base_url() or "").rstrip("/")
    except Exception:
        return ""


def _empty_customer() -> dict:
    return {
        "wallet": 0,
        "wallet_pay_enabled": False,
        "qr_enabled": False,
        "commerce_allowed": False,
        "home": _empty_home(),
    }


async def _admin_ops_payload(session: AsyncSession) -> dict:
    from app.services.home_overview import bot_panel_summary

    try:
        summary = await bot_panel_summary(session)
    except Exception:
        await rollback_quiet(session)
        summary = {
            "users": 0,
            "orders": 0,
            "services": 0,
            "pending": 0,
            "revenue": 0,
            "tickets": 0,
            "resellers": 0,
        }
    base = _mini_panel_base()
    return {
        "stats": {
            "users": int(summary.get("users") or 0),
            "orders": int(summary.get("orders") or 0),
            "services": int(summary.get("services") or 0),
            "pending": int(summary.get("pending") or 0),
            "revenue": int(summary.get("revenue") or 0),
            "tickets": int(summary.get("tickets") or 0),
            "resellers": int(summary.get("resellers") or 0),
        },
        "panel_links": [
            {"id": "resellers", "label": "نمایندگان", "path": "/resellers"},
            {"id": "finance", "label": "مالی", "path": "/finance"},
            {"id": "users", "label": "کاربران", "path": "/users"},
            {"id": "tickets", "label": "تیکت‌ها", "path": "/tickets"},
            {"id": "dashboard", "label": "نمای کلی وب", "path": "/dashboard"},
        ]
        if base
        else [],
        "panel_base": base,
    }


async def _reseller_ops_payload(
    session: AsyncSession, profile: ResellerProfile | None, *, user: BotUser
) -> dict:
    """Shop stats for this reseller only — profile must belong to ``user``."""
    from app.api.home_pages import _reseller_shop_stats
    from app.services.resellers import get_reseller_panel_base_url

    if profile is not None and int(profile.user_id) != int(user.id):
        # Fail closed — never serve another reseller's ops.
        profile = None

    stats = {
        "users": 0,
        "orders": 0,
        "pending": 0,
        "services": 0,
        "revenue": 0,
        "plans": 0,
        "tickets": 0,
    }
    if profile is not None:
        try:
            stats = await _reseller_shop_stats(session, int(profile.user_id))
        except Exception:
            await rollback_quiet(session)
    base = (await get_reseller_panel_base_url(session) or "").rstrip("/") or _mini_panel_base()
    billing = None
    if profile is not None and (profile.billing_mode or "") == "payg":
        billing = {
            "balance": int(profile.billing_balance or 0),
            "suspended": bool(getattr(profile, "billing_suspended_at", None)),
        }
    return {
        "stats": stats,
        "shop": {
            "bot_username": (profile.bot_username if profile else None) or "",
            "active": bool(profile.is_active) if profile else False,
            "billing": billing,
        },
        "panel_links": [
            {"id": "home", "label": "داشبورد وب", "path": "/home"},
            {"id": "finance", "label": "مالی", "path": "/finance"},
            {"id": "tickets", "label": "پشتیبانی", "path": "/tickets"},
            {"id": "shop", "label": "تنظیمات فروشگاه", "path": "/shop-settings"},
        ]
        if base
        else [],
        "panel_base": base,
    }


async def _renewal_already_ordered(session: AsyncSession, user: BotUser, preview: object) -> bool:
    """True when this preview's request key already created the caller's renewal order.

    A retry after a lost response reuses that order (paid or not), so the balance the
    first try spent must not block it; ``pay_with_wallet`` still checks an unpaid one.
    """
    key = preview.get("request_key") if isinstance(preview, dict) else None
    if not isinstance(key, str) or not key:
        return False
    order_id = await session.scalar(
        select(Order.id).where(Order.renewal_request_key == key, Order.user_id == int(user.id)).limit(1)
    )
    return order_id is not None


def register_miniapp_pages(app: FastAPI, *, render, get_db) -> None:
    @app.get("/api/mini/service/{service_id}/cancellation")
    async def mini_cancellation(request: Request, service_id: int, session: AsyncSession = Depends(get_db)):
        from app.services.service_cancellations import cancellation_status
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        await _owned_service_or_404(session, await session.get(UserService, service_id), user)
        try:
            return _no_store({"request": await cancellation_status(session, user, service_id, shop_id=None)})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/mini/service/{service_id}/cancellation")
    async def mini_request_cancellation(request: Request, service_id: int, session: AsyncSession = Depends(get_db)):
        from app.services.service_cancellations import request_cancellation
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        await _owned_service_or_404(session, await session.get(UserService, service_id), user)
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get("reason"), str):
                raise ValueError("دلیل لغو را بنویسید")
            row = await request_cancellation(session, user, service_id, shop_id=None, reason=data["reason"])
            return _no_store({"request": row})
        except ValueError:
            await rollback_quiet(session)
            raise HTTPException(400, "درخواست لغو نامعتبر است؛ دلیل و فروشگاه سرویس را بررسی کنید") from None

    @app.get("/miniapp/", response_class=HTMLResponse)
    @app.get("/miniapp", response_class=HTMLResponse)
    async def miniapp_index(request: Request):
        assert_miniapp_feature_enabled()
        resp = render(request, "miniapp.html", {})
        # Never let intermediaries / SW-ish caches serve a shell without WebApp context.
        try:
            resp.headers["Cache-Control"] = "no-store"
        except Exception:
            log.debug("miniapp_index: ignored failure", exc_info=True)
        return resp

    @app.get("/miniapp/s/{shop_key}/", response_class=HTMLResponse)
    @app.get("/miniapp/s/{shop_key}", response_class=HTMLResponse)
    async def miniapp_shop_index(request: Request, shop_key: str, session: AsyncSession = Depends(get_db)):
        """A shop's Mini App: the same page, served only while that shop's Mini App is on."""
        assert_miniapp_feature_enabled()
        if not KEY_PATTERN.fullmatch(shop_key):
            raise HTTPException(404, SHOP_UNAVAILABLE)
        await resolve_shop_scope(session, shop_key)
        resp = render(request, "miniapp.html", {})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/mini/me")
    async def mini_me(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        scope = mini_scope(request)
        if scope.is_shop:
            return _no_store(await _shop_me_payload(session, user, scope))
        persona = resolve_mini_persona(user)
        payload: dict = {
            "persona": persona,
            "nav": _nav_for(persona),
            "user": {
                "id": user.id,
                "name": user.full_name or "",
                "role": user.role,
                "wallet": int(user.wallet_balance or 0) if commerce_allowed(persona) else 0,
            },
            "currency": get_settings().currency or "تومان",
            "panel_base": _mini_panel_base(),
            "commerce_allowed": commerce_allowed(persona),
            "brand": (await platform_brand(session)).payload(),
        }
        if commerce_allowed(persona):
            payload["customer"] = await _customer_summary(session, user)
        else:
            # Owner/admin: ops only — no commerce surfaces / no foreign service lists
            payload["customer"] = _empty_customer()
        return _no_store(payload)

    @app.get("/api/mini/service/{service_id}")
    async def mini_service(
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        shop_id = mini_scope(request).shop_id
        svc = await _owned_service_or_404(session, await session.get(UserService, service_id), user, shop_id=shop_id)
        info = await _fetch_pg_info(_service_sub_token(svc), subscription_url=svc.subscription_url, service_id=svc.id, expected_pg_user_id=svc.pg_user_id)
        if info.get("status") and "expire" in info:
            from app.services.bot_user_admin import sync_service_quota_cache
            sync_service_quota_cache(svc, info=info)
            await session.commit()
        # Never return raw PG payload — allowlisted summary only
        row = _serialize_service(svc, info)
        row["addons_allowed"] = _addons_allowed(user, svc)
        return _no_store({"service": row})

    @app.get("/api/mini/service/{service_id}/qr")
    async def mini_service_qr(
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.pasarguard import absolutize_subscription_url
        from app.services.qrcode_gen import make_subscription_qr

        user = await load_mini_user(session, request)
        _require_commerce(user)
        shop_id = mini_scope(request).shop_id
        svc = await _owned_service_or_404(session, await session.get(UserService, service_id), user, shop_id=shop_id)
        url = absolutize_subscription_url(svc.subscription_url) or (
            (svc.subscription_url or "").strip()
        )
        if not url:
            raise HTTPException(404, "no subscription url")
        ui = await get_all_settings(session, reseller_id=shop_id)
        if not on(ui.get("qr_enabled", "1")):
            raise HTTPException(403, "qr disabled")
        bg = (ui.get("qr_background") or "").strip() or None
        try:
            buf = make_subscription_qr(url, background=bg, box_size=8, border=2)
        except Exception:
            log.exception("miniapp qr failed service=%s", service_id)
            raise HTTPException(500, "qr failed")
        data = buf.getvalue()
        if request.query_params.get("format") == "png":
            return Response(
                content=data,
                media_type="image/png",
                headers={"Cache-Control": "no-store, private"},
            )
        b64 = base64.b64encode(data).decode("ascii")
        return _no_store({"png_base64": b64, "url": url})

    @app.get("/api/mini/service/{service_id}/addons")
    async def mini_service_addons(
        service_id: int,
        request: Request,
        kind: Literal["volume", "duration"] | None = None,
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.service_addons import amount_label, list_packs_for_service

        user = await load_mini_user(session, request)
        _require_commerce(user)
        svc = await _addon_service(session, user, service_id, shop_id=mini_scope(request).shop_id)
        # Category-matched packs for this service, from the shop that sold it.
        packs = await list_packs_for_service(
            session, svc, active_only=True, kind=kind
        )
        return _no_store(
            {
                "kind": kind,
                "packs": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "kind": p.kind,
                        "amount_label": amount_label(p),
                        "description": p.description or "",
                        **displayed_quote(p).payload(),
                    }
                    for p in packs
                ]
            }
        )

    @app.post("/api/mini/addon")
    async def mini_addon(request: Request, session: AsyncSession = Depends(get_db)):
        from app.db.models import ServiceAddonPack
        from app.services.orders import pay_with_wallet
        from app.services.service_addons import (
            create_addon_order,
            kind_label,
            pack_matches_service_category,
            pack_matches_shop,
            service_plan_category_id,
        )

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        user_id = int(user.id)
        try:
            body = await request.json()
            service_id = int(body.get("service_id"))
            pack_id = int(body.get("pack_id"))
            kind = body.get("kind")
        except (AttributeError, TypeError, ValueError):
            raise HTTPException(400, "سرویس و بسته را انتخاب کنید")
        if kind is not None and kind not in ("volume", "duration"):
            raise HTTPException(400, "نوع بسته نامعتبر است")
        shop_id = mini_scope(request).shop_id
        ui = await _scope_ui(session, request)
        if not on(ui.get("pay_wallet_enabled")):
            raise HTTPException(403, "پرداخت با کیف پول غیرفعال است")
        svc = await _addon_service(session, user, service_id, shop_id=shop_id)
        pack = await session.get(ServiceAddonPack, pack_id)
        if not pack_matches_shop(pack, shop_id):
            raise HTTPException(400, "بسته یافت نشد")
        if kind is not None and pack.kind != kind:
            raise HTTPException(400, "نوع بسته با بخش انتخاب‌شده مطابقت ندارد")
        svc_cat = await service_plan_category_id(session, svc)
        if not pack_matches_service_category(pack, svc_cat):
            raise HTTPException(400, "این بسته برای دستهٔ پلن این سرویس نیست")
        await session.refresh(user)
        quote = await quote_product(session, pack, shop_id=shop_id)
        if quote.price > await _scope_wallet(session, user, request):
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
        try:
            order = await create_addon_order(
                session, user_id=user.id, service=svc, pack=pack, expected_price=body.get("price")
            )
            order = await pay_with_wallet(session, order, user)
            await session.refresh(user)
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="افزایش حجم یا زمان ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini addon failed user=%s svc=%s", user_id, service_id)
            raise HTTPException(500, "افزایش حجم یا زمان ناموفق")
        order_id = int(order.id)
        response = _no_store(
            {
                "ok": True,
                "order_id": order_id,
                "wallet": await _scope_wallet(session, user, request),
                "message": f"افزایش {kind_label(pack.kind)} با موفقیت انجام شد",
            }
        )
        # Notification failures must not turn a completed purchase into an error.
        try:
            from app.services.notifications import notify_new_subscription
            from app.services.reseller_bots import open_notify_bot_for_user

            bot, should_close = await open_notify_bot_for_user(session, user, shop_id=shop_id)
            try:
                await notify_new_subscription(
                    bot, session, order=order, user_tg_id=user.telegram_id
                )
            finally:
                if should_close:
                    await bot.session.close()
        except Exception:
            log.exception("mini addon notification failed order=%s", order_id)
        return response

    @app.post("/api/mini/buy")
    async def mini_buy(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.orders import create_order, get_catalog_plan, pay_with_wallet

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        try:
            plan_id = int(body.get("plan_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "plan_id required")
        ui = await _scope_ui(session, request)
        if not on(ui.get("pay_wallet_enabled")):
            raise HTTPException(403, "پرداخت با کیف پول غیرفعال است")
        # This Mini App's catalog only: load_mini_user set the shop context, and
        # get_catalog_plan refuses every plan another shop (or the platform) sells.
        plan = await get_catalog_plan(session, plan_id)
        if not plan or not plan.is_active:
            raise HTTPException(400, "پلن یافت نشد")
        await session.refresh(user)
        if sale_price(plan) > await _scope_wallet(session, user, request):
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
        try:
            order = await create_order(session, user_id=user.id, plan_id=plan_id, expected_price=body.get("price"))
            await record_order_funnel(session, order, STEP_PAY_START, key=f"pay_start:order:{order.id}")
            await session.refresh(user)
            order = await pay_with_wallet(session, order, user)
            await session.refresh(user)
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="خرید ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini buy failed user=%s plan=%s", user.id, plan_id)
            raise HTTPException(500, "خرید ناموفق")
        if plan.is_trial:
            await notify_trial_claimed_outside_bot(session, order=order, user=user)
        return _no_store(
            {
                "ok": True,
                "order_id": order.id,
                "wallet": await _scope_wallet(session, user, request),
                "message": "خرید با موفقیت انجام شد",
            }
        )

    @app.get("/api/mini/service/{service_id}/automation")
    async def mini_automation(service_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.service_automation import automation_settings

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        await _owned_service_or_404(session, await session.get(UserService, service_id), user)
        try:
            return _no_store(await automation_settings(session, user, service_id))
        except ValueError as exc:
            raise HTTPException(400, _safe_client_message(exc, fallback="دریافت تنظیمات ناموفق")) from exc

    @app.post("/api/mini/service/{service_id}/automation")
    async def mini_automation_save(service_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.service_automation import configure_automation

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        await _owned_service_or_404(session, await session.get(UserService, service_id), user)
        try:
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
                raise ValueError("وضعیت روشن/خاموش نامعتبر است")
            raw_id = body.get("choice_id")
            if raw_id is not None and (type(raw_id) is not int or raw_id <= 0):
                raise ValueError("پلن یا بسته نامعتبر است")
            await configure_automation(session, user, service_id, body.get("action"), enabled=body["enabled"], choice_id=raw_id)
        except ValueError as exc:
            raise HTTPException(400, _safe_client_message(exc, fallback="ذخیره تنظیمات ناموفق")) from exc
        return _no_store({"ok": True, "message": "تنظیمات ذخیره شد"})

    @app.get("/api/mini/service/{service_id}/renewal-preview")
    async def mini_preview_renewal(service_id: int, plan_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.orders import get_catalog_plan
        from app.services.service_renewals import preview_renewal

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        svc = await _owned_service_or_404(
            session, await session.get(UserService, service_id), user, shop_id=mini_scope(request).shop_id
        )
        plan = await get_catalog_plan(session, plan_id)
        try:
            return _no_store(await preview_renewal(session, user_id=user.id, service=svc, plan=plan))
        except ValueError as exc:
            raise HTTPException(400, _safe_client_message(exc, fallback="دریافت پیش‌نمایش ناموفق")) from exc
        except Exception:
            log.exception("mini renewal preview failed user=%s svc=%s", user.id, service_id)
            raise HTTPException(503, "دریافت اطلاعات سرویس ناموفق بود؛ دوباره تلاش کنید") from None

    @app.post("/api/mini/renew")
    async def mini_renew(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.orders import get_catalog_plan, pay_with_wallet, renew_service_with_plan

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("bad json")
        except Exception:
            raise HTTPException(400, "bad json")
        try:
            service_id = int(body.get("service_id"))
            plan_id = int(body.get("plan_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "service_id and plan_id required")
        ui = await _scope_ui(session, request)
        if not on(ui.get("pay_wallet_enabled")):
            raise HTTPException(403, "پرداخت با کیف پول غیرفعال است")
        svc = await _owned_service_or_404(
            session, await session.get(UserService, service_id), user, shop_id=mini_scope(request).shop_id
        )
        if (svc.remark or "").strip() == "linked":
            raise HTTPException(
                400,
                "سرویس متصل‌شده فقط مشاهده است؛ تمدید از این مسیر ممکن نیست",
            )
        plan = await get_catalog_plan(session, plan_id)
        if (
            not plan
            or not plan.is_active
            or plan.is_trial
        ):
            raise HTTPException(400, "پلن تمدید نامعتبر است")
        await session.refresh(user)
        retrying = await _renewal_already_ordered(session, user, body.get("preview"))
        if not retrying and sale_price(plan) > await _scope_wallet(session, user, request):
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
        try:
            from app.services.service_renewals import renewal_terms

            quote = body.get("preview")
            if not isinstance(quote, dict) or quote.get("terms") != renewal_terms(plan) or quote.get("price") != sale_price(plan) or not quote.get("request_key"):
                raise ValueError("پیش‌نمایش تمدید معتبر نیست یا پلن تغییر کرده؛ دوباره پلن را انتخاب کنید")
            order = await renew_service_with_plan(
                session, user_id=user.id, service=svc, plan=plan, request_key=quote["request_key"], expected_price=quote["price"],
            )
            await session.refresh(user)
            order = await pay_with_wallet(session, order, user)
            await session.refresh(user)
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="تمدید ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini renew failed user=%s svc=%s", user.id, service_id)
            raise HTTPException(500, "تمدید ناموفق")
        return _no_store(
            {
                "ok": True,
                "order_id": order.id,
                "wallet": await _scope_wallet(session, user, request),
                "message": "تمدید با موفقیت انجام شد",
            }
        )
