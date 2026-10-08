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
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, Plan, ResellerProfile, UserService
from app.services.db_safe import rollback_quiet
from app.services.formatting import (
    expire_remaining_days,
    format_bytes_ratio,
    format_expire_short,
    hold_duration_from_info,
    is_on_hold_status,
    status_label_plain,
    time_remaining_label,
)
from app.services.miniapp_auth import (
    assert_mini_force_join,
    assert_miniapp_feature_enabled,
    load_mini_user,
    load_reseller_profile,
    resolve_mini_persona,
)
from app.services.pasarguard import get_pg
from app.services.users import get_all_settings, on

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
            {"id": "home", "label": "خانه", "icon": "home"},
            {"id": "ops", "label": "عملیات", "icon": "ops"},
        ]
    base = [
        {"id": "home", "label": "خانه", "icon": "home"},
        {"id": "services", "label": "سرویس", "icon": "svc"},
        {"id": "shop", "label": "خرید", "icon": "shop"},
        {"id": "wallet", "label": "کیف پول", "icon": "wallet"},
    ]
    if persona == "reseller":
        return base + [{"id": "ops", "label": "پنل", "icon": "ops"}]
    return base


def _require_commerce(user: BotUser) -> str:
    persona = resolve_mini_persona(user)
    if not commerce_allowed(persona):
        raise HTTPException(403, "خرید/تمدید برای این نقش در مینی‌اپ مجاز نیست")
    return persona


async def _require_commerce_ready(session: AsyncSession, user: BotUser) -> str:
    """Commerce persona + force-join (users) before mutating wallet/orders."""
    persona = _require_commerce(user)
    await assert_mini_force_join(session, user)
    return persona


def _owned_service_or_404(svc: UserService | None, user: BotUser) -> UserService:
    """Fail closed: service must belong to the authenticated Mini App user."""
    if not svc or int(svc.bot_user_id) != int(user.id):
        raise HTTPException(404)
    return svc


def _addons_allowed(user: BotUser, svc: UserService) -> bool:
    return (
        user.reseller_id is None
        and bool(svc.pg_user_id)
        and (svc.remark or "").strip() != "linked"
    )


async def _addon_service(
    session: AsyncSession, user: BotUser, service_id: int
) -> UserService:
    svc = _owned_service_or_404(await session.get(UserService, service_id), user)
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


async def _fetch_pg_info(
    subscription_token: str | None,
    *,
    subscription_url: str | None = None,
) -> dict:
    """Public subscription info — path from panel URL (auth=False, no admin JWT)."""
    token = (subscription_token or "").strip() or None
    url = (subscription_url or "").strip() or None
    if not token and not url:
        return {"error": "upstream_unavailable"}
    try:
        info = await asyncio.wait_for(
            get_pg().subscription_info(token, subscription_url=url),
            timeout=5.0,
        )
        return info if isinstance(info, dict) and info else {"error": "upstream_unavailable"}
    except Exception:
        return {"error": "upstream_unavailable"}


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


def _serialize_service(svc: UserService, info: dict | None = None) -> dict:
    info = info or {}
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
    volume_known = not upstream_err and "data_limit" in info
    time_known = not upstream_err and (
        "expire" in info or "expire_date" in info or pending
    )
    # Never expose subscription_token — only the share URL the panel produced.
    from app.services.pasarguard import absolutize_subscription_url, user_subscription_url

    live = user_subscription_url(info if isinstance(info, dict) else None)
    if live and live != (svc.subscription_url or ""):
        svc.subscription_url = live
    sub_url = (
        live
        or absolutize_subscription_url(svc.subscription_url)
        or (svc.subscription_url or "")
    )
    return {
        "id": svc.id,
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
        "expire": format_expire_short(expire, status=status_raw, expire_duration=hold_dur)
        if not upstream_err
        else "—",
        "expire_days": days,
        "expire_days_label": time_remaining_label(days_left=days, status=status_raw)
        if time_known
        else "—",
        "pending_start": pending,
        "online_at": format_expire_short(info.get("online_at"))
        if info.get("online_at") and not upstream_err
        else None,
        "error": "upstream_unavailable" if upstream_err else None,
    }


async def _enrich_services(services: list[UserService]) -> list[dict]:
    if not services:
        return []
    try:
        infos = await asyncio.gather(
            *[
                _fetch_pg_info(
                    _service_sub_token(s),
                    subscription_url=getattr(s, "subscription_url", None),
                )
                for s in services[:20]
            ]
        )
    except Exception:
        log.exception("miniapp enrich gather failed; returning bare services")
        infos = [{} for _ in services[:20]]
    out: list[dict] = []
    for s, info in zip(services[:20], infos):
        try:
            out.append(_serialize_service(s, info if isinstance(info, dict) else {}))
        except Exception:
            log.exception("miniapp serialize failed service=%s", getattr(s, "id", None))
            out.append(
                {
                    "id": int(getattr(s, "id", 0) or 0),
                    "username": getattr(s, "pg_username", None) or "",
                    "subscription_url": getattr(s, "subscription_url", None) or "",
                    "plan_id": getattr(s, "plan_id", None),
                    "status": "—",
                    "status_fa": "—",
                    "traffic": "—",
                    "traffic_pct": None,
                    "expire": "—",
                    "expire_days": None,
                    "expire_days_label": "—",
                    "pending_start": False,
                    "online_at": None,
                    "error": "upstream_unavailable",
                }
            )
    for s in services[20:]:
        try:
            out.append(_serialize_service(s, {}))
        except Exception:
            out.append(
                {
                    "id": int(getattr(s, "id", 0) or 0),
                    "username": getattr(s, "pg_username", None) or "",
                    "subscription_url": getattr(s, "subscription_url", None) or "",
                    "plan_id": getattr(s, "plan_id", None),
                    "status": "—",
                    "status_fa": "—",
                    "traffic": "—",
                    "traffic_pct": None,
                    "expire": "—",
                    "expire_days": None,
                    "expire_days_label": "—",
                    "pending_start": False,
                    "online_at": None,
                    "error": None,
                }
            )
    return out


async def _user_shop_payload(session: AsyncSession, user: BotUser) -> dict:
    """Customer commerce payload — caller's services/wallet/activity only.

    Plans are **platform catalog only** (owner_reseller_id IS NULL). Mini App never
    mounts shop-bot context, so foreign reseller catalogs must not appear here.
    """
    from app.services.wallet import list_activity

    svc_result = await session.execute(
        select(UserService)
        .where(UserService.bot_user_id == user.id)
        .order_by(UserService.id.desc())
    )
    services = list(svc_result.scalars().all())
    plans_q = (
        select(Plan)
        .where(
            Plan.is_active.is_(True),
            Plan.is_trial.is_(False),
            Plan.owner_reseller_id.is_(None),
        )
        .order_by(Plan.sort_order, Plan.id)
    )
    plans = list((await session.execute(plans_q)).scalars().all())
    trial = (
        await session.execute(
            select(Plan)
            .where(
                Plan.is_active.is_(True),
                Plan.is_trial.is_(True),
                Plan.owner_reseller_id.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    ui = await get_all_settings(session)
    wallet_pay = on(ui.get("pay_wallet_enabled"))
    try:
        enriched = await _enrich_services(services)
    except Exception:
        log.exception("miniapp enrich failed user=%s", user.id)
        # Still list local rows — never hide owned services because PG enrich failed.
        enriched = [
            {
                "id": int(s.id),
                "username": s.pg_username or "",
                "subscription_url": s.subscription_url or "",
                "plan_id": s.plan_id,
                "status": "—",
                "status_fa": "—",
                "traffic": "—",
                "traffic_pct": None,
                "expire": "—",
                "expire_days": None,
                "expire_days_label": "—",
                "pending_start": False,
                "online_at": None,
                "error": "upstream_unavailable",
            }
            for s in services
        ]
    for row, svc in zip(enriched, services):
        row["addons_allowed"] = _addons_allowed(user, svc)
    try:
        activity = await list_activity(session, user.id, limit=25)
    except Exception:
        await rollback_quiet(session)
        log.exception("miniapp activity failed user=%s", user.id)
        activity = []
    return {
        "wallet": int(user.wallet_balance or 0),
        "wallet_pay_enabled": wallet_pay,
        "commerce_allowed": True,
        "services": enriched,
        "plans": [
            {
                "id": p.id,
                "name": p.name,
                "price": int(p.price or 0),
                "days": p.duration_days,
                "gb": p.data_limit_gb,
                "is_trial": bool(p.is_trial),
            }
            for p in (([trial] if trial else []) + plans)
        ],
        "activity": [
            {
                "amount": int(a.amount),
                "reason": a.reason or "",
                "created_at": a.created_at.astimezone(timezone.utc).isoformat()
                if isinstance(a.created_at, datetime)
                else None,
            }
            for a in activity
        ],
    }


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
        "commerce_allowed": False,
        "services": [],
        "plans": [],
        "activity": [],
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
            stats = await _reseller_shop_stats(session, int(profile.id))
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


def register_miniapp_pages(app: FastAPI, *, render, get_db) -> None:
    @app.get("/miniapp/", response_class=HTMLResponse)
    @app.get("/miniapp", response_class=HTMLResponse)
    async def miniapp_index(request: Request):
        assert_miniapp_feature_enabled()
        return render(request, "miniapp.html", {})

    @app.get("/api/mini/me")
    async def mini_me(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
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
        }
        if commerce_allowed(persona):
            payload["customer"] = await _user_shop_payload(session, user)
        else:
            # Owner/admin: ops only — no commerce surfaces / no foreign service lists
            payload["customer"] = _empty_customer()
        if persona == "admin":
            payload["ops"] = await _admin_ops_payload(session)
        elif persona == "reseller":
            profile = await load_reseller_profile(session, user)
            payload["ops"] = await _reseller_ops_payload(session, profile, user=user)
        ops_base = ((payload.get("ops") or {}).get("panel_base") or "").rstrip("/")
        if ops_base:
            payload["panel_base"] = ops_base
        return _no_store(payload)

    @app.get("/api/mini/service/{service_id}")
    async def mini_service(
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        svc = _owned_service_or_404(await session.get(UserService, service_id), user)
        info = await _fetch_pg_info(
            _service_sub_token(svc),
            subscription_url=svc.subscription_url,
        )
        # Never return raw PG payload — allowlisted summary only
        return _no_store({"service": _serialize_service(svc, info)})

    @app.get("/api/mini/service/{service_id}/qr")
    async def mini_service_qr(
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.pasarguard import absolutize_subscription_url
        from app.services.qrcode_gen import make_subscription_qr

        user = await load_mini_user(session, request)
        _require_commerce(user)
        svc = _owned_service_or_404(await session.get(UserService, service_id), user)
        url = absolutize_subscription_url(svc.subscription_url) or (
            (svc.subscription_url or "").strip()
        )
        if not url:
            raise HTTPException(404, "no subscription url")
        ui = await get_all_settings(session)
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
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.service_addons import amount_label, list_packs

        user = await load_mini_user(session, request)
        _require_commerce(user)
        await _addon_service(session, user, service_id)
        # Explicit platform scope, independent of any shop-bot ContextVar.
        packs = await list_packs(session, None, active_only=True)
        return _no_store(
            {
                "packs": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "kind": p.kind,
                        "amount_label": amount_label(p),
                        "description": p.description or "",
                        "price": int(p.price or 0),
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
            pack_matches_shop,
        )

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        user_id = int(user.id)
        try:
            body = await request.json()
            service_id = int(body.get("service_id"))
            pack_id = int(body.get("pack_id"))
        except (AttributeError, TypeError, ValueError):
            raise HTTPException(400, "سرویس و بسته را انتخاب کنید")
        ui = await get_all_settings(session)
        if not on(ui.get("pay_wallet_enabled")):
            raise HTTPException(403, "پرداخت با کیف پول غیرفعال است")
        svc = await _addon_service(session, user, service_id)
        pack = await session.get(ServiceAddonPack, pack_id)
        if not pack_matches_shop(pack, None):
            raise HTTPException(400, "بسته یافت نشد")
        await session.refresh(user)
        if int(pack.price or 0) > int(user.wallet_balance or 0):
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
        try:
            order = await create_addon_order(
                session, user_id=user.id, service=svc, pack=pack
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
                "wallet": int(user.wallet_balance or 0),
                "message": f"افزایش {kind_label(pack.kind)} با موفقیت انجام شد",
            }
        )
        # Notification failures must not turn a completed purchase into an error.
        try:
            from app.services.notifications import notify_new_subscription
            from app.services.reseller_bots import open_notify_bot_for_user

            bot, should_close = await open_notify_bot_for_user(session, user)
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
        ui = await get_all_settings(session)
        if not on(ui.get("pay_wallet_enabled")):
            raise HTTPException(403, "پرداخت با کیف پول غیرفعال است")
        # Platform catalog only (get_catalog_plan rejects foreign reseller plans
        # when shop ContextVar is unset — Mini App never sets shop context).
        plan = await get_catalog_plan(session, plan_id)
        if not plan or not plan.is_active or plan.owner_reseller_id is not None:
            raise HTTPException(400, "پلن یافت نشد")
        await session.refresh(user)
        if int(plan.price or 0) > int(user.wallet_balance or 0):
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
        try:
            order = await create_order(session, user_id=user.id, plan_id=plan_id)
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
        return _no_store(
            {
                "ok": True,
                "order_id": order.id,
                "wallet": int(user.wallet_balance or 0),
                "message": "خرید با موفقیت انجام شد",
            }
        )

    @app.post("/api/mini/renew")
    async def mini_renew(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.orders import get_catalog_plan, pay_with_wallet, renew_service_with_plan

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        try:
            service_id = int(body.get("service_id"))
            plan_id = int(body.get("plan_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "service_id and plan_id required")
        ui = await get_all_settings(session)
        if not on(ui.get("pay_wallet_enabled")):
            raise HTTPException(403, "پرداخت با کیف پول غیرفعال است")
        svc = _owned_service_or_404(await session.get(UserService, service_id), user)
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
            or plan.owner_reseller_id is not None
        ):
            raise HTTPException(400, "پلن تمدید نامعتبر است")
        await session.refresh(user)
        if int(plan.price or 0) > int(user.wallet_balance or 0):
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
        try:
            order = await renew_service_with_plan(
                session, user_id=user.id, service=svc, plan=plan
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
                "wallet": int(user.wallet_balance or 0),
                "message": "تمدید با موفقیت انجام شد",
            }
        )
