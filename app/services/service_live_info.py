"""Fresh public subscription reads shared by customer bot and Mini App views."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.services.formatting import (
    hold_duration_from_info,
    is_on_hold_status,
    parse_expire,
)
from app.services.pasarguard import PasarGuardError, extract_sub_token, get_pg

log = logging.getLogger(__name__)

ERROR_MESSAGES = {
    "missing_subscription_token": "لینک اشتراک این سرویس ثبت نشده است. به پشتیبانی خبر دهید.",
    "timeout": "پنل در زمان مقرر پاسخ نداد؛ دوباره تلاش کنید.",
    "subscription_not_found": "اطلاعات این سرویس روی سرور پیدا نشد. ممکن است سرویس حذف شده یا لینک آن تغییر کرده باشد؛ به پشتیبانی خبر دهید.",
    "access_denied": "سرور اجازه نمایش اطلاعات این سرویس را نداد. به پشتیبانی خبر دهید.",
    "invalid_response": "پاسخ سرور برای این سرویس معتبر نبود. کمی بعد دوباره تلاش کنید یا به پشتیبانی خبر دهید.",
    "incomplete_response": "پاسخ سرور ناقص بود و بعضی اطلاعات سرویس دریافت نشد.",
    "service_mismatch": "اطلاعات دریافتی با این سرویس مطابقت ندارد. به پشتیبانی خبر دهید.",
    "not_loaded": "اطلاعات این سرویس هنوز دریافت نشده؛ دکمه به‌روزرسانی را بزنید.",
    "upstream_unavailable": "دریافت اطلاعات سرویس ممکن نشد. کمی بعد دوباره تلاش کنید.",
}
# Only when the panel did not answer; a 404, a denial or a mismatch means the
# saved values may describe a service that no longer exists as shown.
STORED_QUOTA_FALLBACK_CODES = frozenset({"timeout", "upstream_unavailable"})


def service_info_error(code: str = "upstream_unavailable") -> dict[str, Any]:
    code = (
        code
        if isinstance(code, str) and code in ERROR_MESSAGES
        else "upstream_unavailable"
    )
    return {
        "error": "upstream_unavailable",
        "error_code": code,
        "error_message": ERROR_MESSAGES[code],
    }


def validate_service_info(payload: Any) -> dict[str, Any]:
    """Keep received fields; omitted or malformed quotas remain unknown."""
    if not isinstance(payload, dict) or not payload:
        return service_info_error("invalid_response")
    if payload.get("error") == "upstream_unavailable":
        return service_info_error(payload.get("error_code", "upstream_unavailable"))
    info = dict(payload)
    for key in ("error", "error_code", "error_message"):
        info.pop(key, None)
    missing = []
    status = str(info.get("status") or "").strip().lower().replace("-", "_")
    if is_on_hold_status(status):
        status = "on_hold"
    if status in {"active", "on_hold", "disabled", "limited", "expired"}:
        info["status"] = status
    else:
        info.pop("status", None)
        missing.append("وضعیت")
    for key, label in (("used_traffic", "حجم مصرف‌شده"), ("data_limit", "سقف حجم")):
        value = info.get(key)
        try:
            if (
                key not in info
                or isinstance(value, bool)
                or (value is None and key == "used_traffic")
            ):
                raise ValueError
            if value is not None:
                number = float(value)
                if not math.isfinite(number) or number < 0 or number != int(number):
                    raise ValueError
                info[key] = int(number)
        except (ValueError, TypeError, OverflowError):
            info.pop(key, None)
            missing.append(label)
    expire_key = "expire" if "expire" in info else "expire_date"
    expire = info.get(expire_key)
    expire_known = expire_key in info
    if expire_known and (isinstance(expire, bool) or expire not in (None, 0)):
        try:
            if isinstance(expire, bool) or parse_expire(expire) is None:
                raise ValueError
        except (ValueError, TypeError, OverflowError, OSError):
            info.pop(expire_key, None)
            expire_known = False
    if is_on_hold_status(status) and expire in (None, 0):
        if hold_duration_from_info(info) is None:
            missing.append("مدت پس از اتصال")
    elif not expire_known:
        missing.append("زمان انقضا")
    if missing:
        if not any(key in info for key in ("status", "used_traffic", "data_limit")):
            return service_info_error("invalid_response")
        info.update(
            error="incomplete_response",
            error_code="incomplete_response",
            error_message="پاسخ پنل ناقص است؛ دریافت نشد: " + "، ".join(missing) + ".",
        )
    return info


async def fetch_subscription_info(
    token: str | None,
    *,
    subscription_url: str | None = None,
    service_id: int | None = None,
    expected_pg_user_id: int | None = None,
    client_factory: Callable | None = None,
) -> dict[str, Any]:
    token = (token or "").strip() or extract_sub_token(subscription_url)
    if not token:
        info = service_info_error("missing_subscription_token")
    else:
        try:
            pg = (client_factory or get_pg)()
            call = (
                pg.subscription_info(token, subscription_url=subscription_url)
                if subscription_url
                else pg.subscription_info(token)
            )
            payload = await asyncio.wait_for(call, timeout=5.0)
            info = validate_service_info(payload)
            if (
                expected_pg_user_id
                and isinstance(payload, dict)
                and payload.get("id") is not None
            ):
                if str(payload["id"]) != str(expected_pg_user_id):
                    info = service_info_error("service_mismatch")
            if info.get("error") != "upstream_unavailable":
                info["info_fetched_at"] = datetime.now(timezone.utc).isoformat()
        except (asyncio.TimeoutError, httpx.TimeoutException):
            info = service_info_error("timeout")
        except PasarGuardError as exc:
            code = (
                "subscription_not_found"
                if exc.status_code == 404
                else "access_denied"
                if exc.status_code in (401, 403)
                else "upstream_unavailable"
            )
            info = service_info_error(code)
        except Exception:
            info = service_info_error()
    if info.get("error"):
        # Never log exception text, request URLs, or subscription tokens.
        log.warning(
            "service info failed service=%s code=%s", service_id, info["error_code"]
        )
    return info


def with_stored_quota(info: dict[str, Any], service: Any) -> dict[str, Any]:
    """Add the expiry and volume limit saved at the last successful panel read.

    For display only: the status stays unknown and usage is not invented. The
    caller must not persist the result back into the quota cache.
    """
    if info.get("error_code") not in STORED_QUOTA_FALLBACK_CODES:
        return info
    synced_at = getattr(service, "quota_synced_at", None)
    if not isinstance(synced_at, datetime):
        return info
    stored = dict(info)
    expire_at = getattr(service, "quota_expire_at", None)
    # Synced with no expiry means the panel reported an unlimited service.
    stored["expire"] = int(_as_utc(expire_at).timestamp()) if isinstance(expire_at, datetime) else 0
    limit = getattr(service, "quota_data_limit_bytes", None)
    if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
        stored["stored_data_limit"] = limit
    stored["stored_at"] = _as_utc(synced_at).isoformat()
    return stored


def _as_utc(moment: datetime) -> datetime:
    # SQLite returns naive values for columns the app writes in UTC.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


async def fetch_live_service_info(
    service, *, client_factory: Callable | None = None
) -> dict[str, Any]:
    return await fetch_subscription_info(
        getattr(service, "subscription_token", None),
        subscription_url=getattr(service, "subscription_url", None),
        service_id=getattr(service, "id", None),
        expected_pg_user_id=getattr(service, "pg_user_id", None),
        client_factory=client_factory,
    )
