"""Panel inbox: notifications + action center (sidebar badge + /inbox page)."""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerProfile
from app.services.payg_charges import count_review_charges
from app.services.shop_scope import is_platform_admin, shop_owner_id

logger = logging.getLogger(__name__)

_EMPTY_ACTION = {"entries": [], "has_items": False}
_EMPTY_PAYG = {"suspended": [], "low": [], "threshold": 0, "review": 0, "has_items": False}

# Soft cache for sidebar dot — avoid rebuilding action center on every navigation.
_SIDEBAR_CACHE: dict[str, tuple[float, bool]] = {}
_SIDEBAR_TTL_SEC = 45.0


def _cache_key(staff: dict) -> str:
    role = str(staff.get("role") or "")
    uid = staff.get("bot_user_id") or staff.get("id") or staff.get("username") or ""
    return f"{role}:{uid}"


def invalidate_inbox_sidebar_cache(staff: dict | None = None) -> None:
    if staff is None:
        _SIDEBAR_CACHE.clear()
        return
    _SIDEBAR_CACHE.pop(_cache_key(staff), None)


def inbox_alert_count(ctx: dict[str, Any]) -> int:
    """Count visible inbox items (same rules as the page)."""
    n = 0
    update = ctx.get("update") or {}
    if update.get("update_available"):
        n += 1
    if ctx.get("ticket_alert"):
        n += 1
    if ctx.get("shop_maintenance"):
        n += 1
    if ctx.get("capacity_warn"):
        n += 1
    pr = ctx.get("payg_risk") or {}
    if pr.get("suspended"):
        n += 1
    if pr.get("low"):
        n += 1
    if pr.get("review"):
        n += 1
    ac = ctx.get("action_center") or {}
    if ac.get("has_items"):
        n += len(ac.get("entries") or [])
    return n


async def _payg_risk_strip(session: AsyncSession) -> dict:
    """Platform-admin strip: suspended + low-balance PAYG resellers."""
    from app.services.billing import (
        BILLING_MODE_PAYG,
        get_low_balance_threshold,
        payg_available_balance,
    )
    from app.services.db_safe import rollback_quiet

    out: dict = {
        "suspended": [],
        "low": [],
        "threshold": 0,
        "review": 0,
        "has_items": False,
    }
    try:
        threshold = await get_low_balance_threshold(session)
    except Exception:
        await rollback_quiet(session)
        threshold = 0
    out["threshold"] = int(threshold or 0)
    try:
        profiles = list(
            (
                await session.execute(
                    select(ResellerProfile).where(
                        ResellerProfile.is_active.is_(True),
                        ResellerProfile.billing_mode == BILLING_MODE_PAYG,
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.exception("payg risk: list profiles failed")
        await rollback_quiet(session)
        return out

    user_ids = [int(p.user_id) for p in profiles if p.user_id]
    users: dict[int, BotUser] = {}
    if user_ids:
        try:
            users = {
                int(u.id): u
                for u in (
                    await session.execute(select(BotUser).where(BotUser.id.in_(user_ids)))
                )
                .scalars()
                .all()
            }
        except Exception:
            logger.exception("payg risk: load users failed")
            await rollback_quiet(session)
            users = {}

    for profile in profiles:
        uid = int(profile.user_id)
        user = users.get(uid)
        label = (user.full_name or user.username or str(uid)) if user else str(uid)
        item = {"user_id": uid, "label": label, "balance": 0}
        if getattr(profile, "billing_suspended_at", None):
            try:
                item["balance"] = await payg_available_balance(session, profile)
            except Exception:
                item["balance"] = int(getattr(profile, "billing_balance", 0) or 0)
            out["suspended"].append(item)
            continue
        try:
            bal = await payg_available_balance(session, profile)
        except Exception:
            bal = int(getattr(profile, "billing_balance", 0) or 0)
        item["balance"] = bal
        if out["threshold"] > 0 and bal <= out["threshold"]:
            out["low"].append(item)

    out["suspended"].sort(key=lambda x: x["balance"])
    out["low"].sort(key=lambda x: x["balance"])
    out["review"] = await count_review_charges(session)
    out["has_items"] = bool(out["suspended"] or out["low"] or out["review"])
    return out


async def _safe_action_center(
    session: AsyncSession, *, reseller_id: int | None, expire_days: int
) -> dict:
    from app.services.db_safe import rollback_quiet
    from app.services.ux20 import build_action_center

    try:
        return await build_action_center(
            session, reseller_id=reseller_id, expire_days=expire_days
        )
    except Exception:
        logger.exception("action_center failed reseller_id=%s", reseller_id)
        await rollback_quiet(session)
        return dict(_EMPTY_ACTION)


async def build_inbox_context(
    session: AsyncSession,
    request,
    staff: dict,
) -> dict[str, Any]:
    """Full context for /inbox — never includes bot tokens or secrets."""
    from app.api.panel_tickets_pages import panel_ticket_dashboard_alert
    from app.services.db_safe import rollback_quiet
    from app.services.users import get_all_settings, on

    update = None
    try:
        from app.services.updates import check_github_update

        # Only the Owner may act on a new version; nobody else is told about it.
        if is_platform_admin(staff):
            update = await check_github_update(force=False)
    except Exception:
        update = None

    try:
        ticket_alert = await panel_ticket_dashboard_alert(
            session,
            staff,
            unread=getattr(request.state, "panel_tickets_unread", None),
        )
    except Exception:
        logger.exception("inbox ticket_alert failed")
        await rollback_quiet(session)
        ticket_alert = None

    # NOTE: shop_owner_id() returns None both for the platform admin AND for a
    # non-admin session with no shop (e.g. pg_staff). Those two must never be
    # conflated — a scopeless non-admin must get empty/default data, never the
    # platform-wide settings/action-center (see app.services.shop_scope docstring).
    admin_scope = is_platform_admin(staff)
    rid = None if admin_scope else shop_owner_id(staff)
    has_scope = admin_scope or rid is not None

    ui: dict = {}
    if has_scope:
        try:
            ui = await get_all_settings(session, reseller_id=int(rid) if rid else None)
        except Exception:
            logger.exception("inbox get_all_settings failed")
            await rollback_quiet(session)
            ui = {}

    try:
        expire_days = int(ui.get("action_center_expire_days") or 3)
    except Exception:
        expire_days = 3

    action_center = dict(_EMPTY_ACTION)
    action_center_ok = True
    if has_scope:
        try:
            from app.services.ux20 import build_action_center

            action_center = await build_action_center(
                session,
                reseller_id=int(rid) if rid else None,
                expire_days=expire_days,
            )
        except Exception:
            logger.exception("inbox action_center failed reseller_id=%s", rid)
            await rollback_quiet(session)
            action_center = dict(_EMPTY_ACTION)
            action_center_ok = False

    shop_maintenance = on(ui.get("shop_maintenance_enabled"))
    capacity_warn = False
    payg_risk = dict(_EMPTY_PAYG)

    if is_platform_admin(staff):
        try:
            payg_risk = await _payg_risk_strip(session)
        except Exception:
            logger.exception("inbox payg_risk failed")
            await rollback_quiet(session)
            payg_risk = dict(_EMPTY_PAYG)
    elif rid and staff.get("pg_admin_username"):
        try:
            from app.services.pg_overview import build_reseller_pg_overview
            from app.services.ux20 import capacity_should_warn

            ov = await build_reseller_pg_overview(staff, session=session)
            if ov.get("ready"):
                try:
                    thr = float(ui.get("capacity_warn_pct") or 80)
                except Exception:
                    thr = 80.0
                capacity_warn = capacity_should_warn(
                    [ov.get("users"), ov.get("traffic")], thr
                )
        except Exception:
            logger.exception("inbox capacity_warn failed rid=%s", rid)
            await rollback_quiet(session)
            capacity_warn = False

    ctx = {
        "staff": staff,
        "update": update,
        "ticket_alert": ticket_alert,
        "action_center": action_center or dict(_EMPTY_ACTION),
        "shop_maintenance": shop_maintenance,
        "capacity_warn": capacity_warn,
        "payg_risk": payg_risk,
        "inbox_count": 0,
        "inbox_has": False,
        "action_center_ok": action_center_ok,
        "inbox_hidden_by_dismiss": 0,
    }
    ctx["inbox_count"] = inbox_alert_count(ctx)
    ctx["inbox_has"] = ctx["inbox_count"] > 0
    raw_count = ctx["inbox_count"]

    try:
        from app.services.inbox_dismissals import (
            cleanup_resolved_dismissals,
            filter_inbox_context,
            load_dismissals,
        )

        await cleanup_resolved_dismissals(session, staff, ctx)
        dismissals = await load_dismissals(session, staff)
        if dismissals:
            ctx = filter_inbox_context(ctx, dismissals)
            ctx["inbox_hidden_by_dismiss"] = max(0, raw_count - int(ctx.get("inbox_count") or 0))
    except Exception:
        logger.exception("inbox dismissals filter failed")
        await rollback_quiet(session)

    # Refresh sidebar cache from authoritative page build.
    _SIDEBAR_CACHE[_cache_key(staff)] = (time.monotonic(), bool(ctx["inbox_has"]))
    return ctx


async def sidebar_inbox_has_alerts(
    session: AsyncSession,
    request,
    staff: dict,
    *,
    force: bool = False,
) -> bool:
    """Boolean for sidebar nav-dot (cached briefly)."""
    key = _cache_key(staff)
    now = time.monotonic()
    if not force:
        hit = _SIDEBAR_CACHE.get(key)
        if hit and (now - hit[0]) < _SIDEBAR_TTL_SEC:
            return hit[1]

    try:
        ctx = await build_inbox_context(session, request, staff)
        has = bool(ctx.get("inbox_has"))
    except Exception:
        logger.exception("sidebar_inbox_has_alerts failed")
        has = False

    _SIDEBAR_CACHE[key] = (now, has)
    return has
