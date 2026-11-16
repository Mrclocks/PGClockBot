"""Operator side of «افزودن سرویس قبلی»: connection status, a link test and the recent tries.

Everything here is scoped to one shop (``None`` = the platform). A shop owner never sees the panel
admins or the owner admin of a service; only the platform Owner does.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ServiceImportAttempt
from app.services.authz import authz_from_staff, can_shop
from app.services.pasarguard import PasarGuardError
from app.services.shop_membership import shop_member_clause
from app.services.service_import import (
    Reason,
    ServiceImportError,
    allowed_admins,
    clear_identity_cache,
    existing_rows,
    own_admin_identity,
    panel_client,
    verify_link,
)
from app.services.service_import_config import allowed_paths, read_settings
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.users import get_all_settings

RECENT_LIMIT = 20
TEST_BUDGET = 20
TEST_WINDOW_SECONDS = 60.0

REASON_LABELS: dict[str, str] = {
    Reason.INVALID_LINK.value: "قالب لینک درست نیست (لینک کامل اشتراک یا فقط توکن)",
    Reason.PATH_NOT_ALLOWED.value: "مسیر لینک در «مسیرهای مجاز» نیست",
    Reason.NOT_FOUND.value: "پنل این لینک را نشناخت (توکن اشتباه، سرویس حذف‌شده یا مسیر اشتباه)",
    Reason.ADMIN_NOT_ALLOWED.value: "سرویس زیر ادمینی است که مجاز نیست",
    Reason.ALREADY_TAKEN.value: "سرویس قبلاً به مشتری دیگری وصل شده",
    Reason.WRONG_SHOP.value: "مشتری از فروشگاه دیگری است",
    Reason.BLOCKED.value: "مشتری مسدود است",
    Reason.LIMIT_REACHED.value: "مشتری به سقف تعداد سرویس رسیده",
    Reason.RATE_LIMITED.value: "تلاش ناموفق زیاد در یک ساعت",
    Reason.PANEL_ERROR.value: "پنل پاسخ نداد یا خطا داد",
    Reason.IDENTITY_UNKNOWN.value: "ادمین ربات در پنل مشخص نشد",
}

_test_calls: dict[str, list[float]] = {}


def import_scope(staff: Mapping[str, Any], perm: str = "shop_settings") -> int | None:
    """Shop whose import settings ``staff`` may see (``None`` = platform). Raises ``PermissionError``."""
    if is_platform_admin(dict(staff)):
        return None
    if not can_shop(authz_from_staff(staff), perm):
        raise PermissionError("دسترسی تنظیمات فروشگاه ندارید")
    shop_id = shop_owner_id(dict(staff))
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def lookup_budget_left(scope: int | None) -> bool:
    """Spend one staff panel lookup of this shop; ``False`` once the minute's budget is used up."""
    key = str(scope)
    now = time.monotonic()
    calls = [t for t in _test_calls.get(key, []) if now - t < TEST_WINDOW_SECONDS]
    if len(calls) >= TEST_BUDGET:
        _test_calls[key] = calls
        return False
    calls.append(now)
    _test_calls[key] = calls
    return True


async def status_snapshot(session: AsyncSession, scope: int | None, *, refresh: bool = False) -> dict[str, Any]:
    """How the bot signs in to the panel and what a link may use; a shop sees only its own admin."""
    config = read_settings(await get_all_settings(session, reseller_id=scope))
    out: dict[str, Any] = {"ok": True, "enabled": config.enabled, "paths": list(allowed_paths(config)), "platform": scope is None}
    try:
        pg = await panel_client(session, scope)
        if refresh:
            clear_identity_cache()
        admins = await allowed_admins(session, pg, scope, config, refresh=refresh)
        out["login"] = "api_key" if getattr(pg, "uses_api_key", False) else "password"
        if scope is None:
            out["signed_in_as"] = await own_admin_identity(pg)
            out["admins"] = sorted(admins)
        else:
            out["admins"] = sorted(admins)
        out["identity_ok"] = bool(admins)
    except (PasarGuardError, httpx.HTTPError, TimeoutError):
        out.update(ok=False, identity_ok=False, error="اتصال به پنل برقرار نشد یا ادمین این فروشگاه تعریف نشده است.")
    return out


async def test_link(session: AsyncSession, scope: int | None, raw_link: str) -> dict[str, Any]:
    """Run the import checks on a link without attaching anything and say exactly why it fails."""
    if not lookup_budget_left(scope):
        return {"ok": False, "reason": Reason.RATE_LIMITED.value, "label": "تست‌های زیاد؛ کمی بعد دوباره تلاش کنید"}
    config = read_settings(await get_all_settings(session, reseller_id=scope))
    try:
        verified = await verify_link(session, raw_link=raw_link, shop_id=scope, config=config, refresh=True)
    except ServiceImportError as exc:
        out: dict[str, Any] = {"ok": False, "reason": exc.reason.value, "label": REASON_LABELS.get(exc.reason.value, "")}
        if scope is None and exc.detail.get("owner_admin"):
            out["owner_admin"] = exc.detail["owner_admin"]
        return out
    except (PasarGuardError, httpx.HTTPError, TimeoutError):
        return {"ok": False, "reason": Reason.PANEL_ERROR.value, "label": REASON_LABELS[Reason.PANEL_ERROR.value]}
    taken = await existing_rows(session, verified.panel_id, verified.token)
    out = {"ok": True, "path": verified.prefix, "already_attached": bool(taken)}
    if scope is None:
        out["panel_username"] = str(verified.record.get("username") or "")
    return out


async def recent_attempts(session: AsyncSession, scope: int | None, *, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    """Newest tries of this shop's customers (SQL-scoped); never a token, only its outcome and reason."""
    query = (
        select(ServiceImportAttempt, BotUser.telegram_id, BotUser.full_name)
        .join(BotUser, BotUser.id == ServiceImportAttempt.bot_user_id)
        .order_by(ServiceImportAttempt.id.desc())
        .limit(max(1, int(limit)))
    )
    query = query.where(shop_member_clause(scope))
    rows = (await session.execute(query)).all()
    return [
        {
            "at": row.created_at.isoformat() if row.created_at else "",
            "customer": int(telegram_id or 0),
            "name": str(full_name or "")[:60],
            "outcome": row.outcome,
            "label": REASON_LABELS.get(row.reason or "", ""),
        }
        for row, telegram_id, full_name in rows
    ]
