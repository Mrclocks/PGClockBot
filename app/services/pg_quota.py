"""Enforce PasarGuard admin role quotas for restricted staff.

Mirrors PasarGuard ``RoleLimits`` + limited-admin write gate so resellers
and credentialed staff cannot exceed the same role limits when acting
through PGClock.

Quota load/enforce uses the actor's own PasarGuard client (env client for
Hybrid Owner / explicit Owner Principal). Never Owner ``get_pg()`` to read
another admin's limits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.services.integration_capacity import durable_user_slots
from app.services.integration_runtime import integration_capacity_retained
from app.services.shop_scope import shop_owner_id
from app.services.formatting import format_bytes


class PgQuotaError(Exception):
    """Raised when a staff action would violate PasarGuard admin quotas."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _format_duration_fa(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "—"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if mins and not days:
        parts.append(f"{mins} دقیقه")
    if not parts:
        return "کمتر از یک دقیقه"
    return " و ".join(parts[:2])


def merge_role_limits(admin: dict | None, role: dict | None) -> dict[str, Any]:
    """Merge role.limits with admin.permission_overrides (overrides win when set).

    Same resolution order as PasarGuard ``get_effective_limits``.
    """
    limits: dict[str, Any] = {}
    role_limits = None
    if isinstance(role, dict):
        role_limits = role.get("limits")
    if role_limits is None and isinstance(admin, dict):
        embedded = admin.get("role")
        if isinstance(embedded, dict):
            role_limits = embedded.get("limits")
    if isinstance(role_limits, dict):
        limits.update(role_limits)

    overrides = (admin or {}).get("permission_overrides") if isinstance(admin, dict) else None
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if v is not None:
                limits[k] = v
    return limits


_DATA_LIMIT_KEYS = ("data_limit", "max_traffic", "traffic_limit")
_USER_LIMIT_KEYS = ("max_users", "users_max")


def _first_positive(source: dict[str, Any] | None, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = _as_int((source or {}).get(key))
        if value and value > 0:
            return value
    return 0


def extract_admin_capacity(admin: dict | None, role: dict | None = None) -> tuple[int, int]:
    """Traffic limit in bytes and user limit of a PasarGuard admin; ``0`` means unlimited.

    One reader for every screen and for the reseller grant. Order matches what the bot writes
    back: the admin's own ``data_limit`` first, then role limits and overrides; for users the
    overrides and role limits first, then the admin's own ``max_users``.
    """
    if not isinstance(admin, dict):
        return 0, 0
    limits = merge_role_limits(admin, role)
    data_limit = _first_positive(admin, _DATA_LIMIT_KEYS) or _first_positive(limits, _DATA_LIMIT_KEYS)
    max_users = _first_positive(limits, _USER_LIMIT_KEYS) or _first_positive(admin, _USER_LIMIT_KEYS)
    return data_limit, max_users


def hwid_bounds(limits: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return (min_hwid, max_hwid) from role limits (aliases supported)."""
    hmin = (
        _as_int(limits.get("min_hwid_per_user"))
        or _as_int(limits.get("hwid_limit_min"))
        or _as_int(limits.get("min_devices"))
    )
    hmax = (
        _as_int(limits.get("max_hwid_per_user"))
        or _as_int(limits.get("hwid_limit_max"))
        or _as_int(limits.get("max_devices"))
        or _as_int(limits.get("device_limit"))
    )
    return hmin, hmax


def _admin_is_limited(admin: dict) -> bool:
    status = str(admin.get("status") or "").strip().lower()
    if status == "limited" or admin.get("is_limited") is True:
        return True
    # Defensive: treat exhausted admin traffic as limited even if status lags.
    data_limit = _as_int(admin.get("data_limit"))
    used = _as_int(admin.get("used_traffic")) or _as_int(admin.get("traffic_used")) or 0
    if data_limit is not None and data_limit > 0 and used >= data_limit:
        return True
    return False


def _admin_is_disabled(admin: dict) -> bool:
    if admin.get("enabled") is False or admin.get("is_disabled") is True:
        return True
    if admin.get("is_active") is False:
        return True
    status = str(admin.get("status") or "").strip().lower()
    return status in {"disabled", "inactive", "deleted", "banned"}


def assert_admin_can_write(admin: dict | None, role: dict | None = None) -> None:
    """Block mutations when the PG admin is disabled or limited (PG write gate)."""
    if not isinstance(admin, dict) or not admin:
        raise PgQuotaError("ادمین پاسارگارد برای این حساب یافت نشد")
    if _admin_is_disabled(admin):
        raise PgQuotaError("حساب ادمین پاسارگارد شما غیرفعال است و امکان تغییر ندارید")
    if _admin_is_limited(admin):
        disabled_when = False
        for src in (role, admin.get("role") if isinstance(admin.get("role"), dict) else None):
            if isinstance(src, dict) and src.get("disabled_when_limited"):
                disabled_when = True
                break
        if disabled_when:
            raise PgQuotaError(
                "حساب شما محدود شده و دسترسی کامل قطع است — با ادمین اصلی تماس بگیرید"
            )
        raise PgQuotaError(
            "حساب شما محدود شده است (سهمیه پر شده) و امکان ساخت یا تغییر کاربر ندارید"
        )


def _check_data_limit_bounds(
    limits: dict[str, Any],
    data_limit: int | None,
    *,
    require_finite: bool,
) -> None:
    data_max = _as_int(limits.get("data_limit_max"))
    data_min = _as_int(limits.get("data_limit_min"))
    unlimited = data_limit is None or data_limit <= 0

    if data_max is not None and data_max > 0 and require_finite and unlimited:
        raise PgQuotaError(
            f"حجم کاربر نمی‌تواند نامحدود باشد؛ حداکثر {format_bytes(data_max)}"
        )
    if not unlimited:
        assert data_limit is not None
        if data_min is not None and data_min > 0 and data_limit < data_min:
            raise PgQuotaError(
                f"حجم کاربر باید حداقل {format_bytes(data_min)} باشد"
            )
        if data_max is not None and data_max > 0 and data_limit > data_max:
            raise PgQuotaError(
                f"حجم کاربر نمی‌تواند بیشتر از {format_bytes(data_max)} باشد"
            )


def _check_expire_bounds(
    limits: dict[str, Any],
    expire_ts: int | None,
    *,
    require_finite: bool,
) -> None:
    expire_max = _as_int(limits.get("expire_max"))
    expire_min = _as_int(limits.get("expire_min"))
    unlimited = expire_ts is None or expire_ts <= 0

    if expire_max is not None and expire_max > 0 and require_finite and unlimited:
        raise PgQuotaError(
            f"مدت کاربر نمی‌تواند نامحدود باشد؛ حداکثر {_format_duration_fa(expire_max)} از الان"
        )
    if not unlimited:
        assert expire_ts is not None
        now = datetime.now(timezone.utc).timestamp()
        seconds = float(expire_ts) - now
        if expire_min is not None and expire_min > 0 and seconds < expire_min:
            raise PgQuotaError(
                f"مدت کاربر باید حداقل {_format_duration_fa(expire_min)} از الان باشد"
            )
        if expire_max is not None and expire_max > 0 and seconds > expire_max:
            raise PgQuotaError(
                f"مدت کاربر نمی‌تواند بیشتر از {_format_duration_fa(expire_max)} از الان باشد"
            )


def _check_hwid_bounds(
    limits: dict[str, Any],
    hwid_limit: int | None,
    *,
    require_finite: bool,
) -> None:
    """Enforce PasarGuard RoleLimits min/max HWID (device) per user."""
    hmin, hmax = hwid_bounds(limits)
    # None or 0 ⇒ unlimited (common PG / panel convention)
    unlimited = hwid_limit is None or hwid_limit <= 0

    if hmax is not None and hmax > 0 and require_finite and unlimited:
        raise PgQuotaError(
            f"سقف دستگاه (HWID) نمی‌تواند نامحدود باشد؛ حداکثر {hmax}"
        )
    if not unlimited:
        assert hwid_limit is not None
        if hmin is not None and hmin > 0 and hwid_limit < hmin:
            raise PgQuotaError(f"سقف دستگاه (HWID) باید حداقل {hmin} باشد")
        if hmax is not None and hmax > 0 and hwid_limit > hmax:
            raise PgQuotaError(f"سقف دستگاه (HWID) نمی‌تواند بیشتر از {hmax} باشد")


def _check_max_users(admin: dict, limits: dict[str, Any], *, need: int = 1) -> None:
    max_users = _as_int(limits.get("max_users")) or _as_int(admin.get("max_users"))
    if max_users is None or max_users <= 0:
        return
    current = (
        _as_int(admin.get("total_users"))
        or _as_int(admin.get("users_count"))
        or 0
    )
    need_n = max(1, int(need or 1))
    if current + need_n - 1 >= max_users:
        remain = max(0, max_users - current)
        raise PgQuotaError(
            f"سقف تعداد کاربران شما پر شده است (حداکثر {max_users}"
            + (f" — ظرفیت باقی‌مانده {remain}" if remain else "")
            + ")"
        )


def _staff_uses_env_pg_client(staff: dict) -> bool:
    """True when this staff's PG identity is the process env client.

    Explicit Owner Principal and Hybrid Owner (``role=admin`` +
    ``pg_is_owner is False``) both authenticate as the ``.env`` PasarGuard
    account — ``get_pg()`` is *their* client, not a parent token.
    """
    from app.services.shop_scope import is_platform_admin

    if is_platform_admin(staff):
        return True
    return staff.get("role") == "admin" and staff.get("pg_is_owner") is False


async def _quota_pg_client(
    staff: dict,
    *,
    session: Any | None = None,
    client: Any | None = None,
):
    """Return the actor's own PG client. Never Owner token for another admin."""
    if client is not None:
        return client
    if _staff_uses_env_pg_client(staff):
        from app.services.pasarguard import get_pg

        return get_pg()
    from app.services.pg_read import PgReadDenied, staff_pg_read_client

    try:
        return await staff_pg_read_client(session, staff)
    except PgReadDenied as e:
        raise PgQuotaError(e.message) from e


async def _client_for_named_pg_admin(
    *,
    session: Any | None,
    username: str,
    reseller_user_id: int | None = None,
    client: Any | None = None,
):
    """Own-credential client for a linked shop/staff PG admin. Never Owner token."""
    if client is not None:
        return client
    from app.services.pasarguard import (
        PasarGuardError,
        get_pg_for_reseller,
        get_pg_for_staff,
    )

    try:
        if reseller_user_id:
            if session is None:
                raise PgQuotaError("نشست پایگاه‌داده برای خواندن سقف فروشگاه لازم است")
            return await get_pg_for_reseller(session, int(reseller_user_id))
        if session is None:
            raise PgQuotaError(
                "خواندن سقف پاسارگارد بدون اعتبارنامه این حساب ممکن نیست"
            )
        from sqlalchemy import func, select

        from app.db.models import ResellerProfile

        uname = str(username).strip().lower()
        profile = (
            await session.execute(
                select(ResellerProfile).where(
                    func.lower(ResellerProfile.pg_admin_username) == uname
                )
            )
        ).scalar_one_or_none()
        if profile and getattr(profile, "user_id", None):
            return await get_pg_for_reseller(session, int(profile.user_id))
        return await get_pg_for_staff(session, pg_username=username)
    except PgQuotaError:
        raise
    except PasarGuardError as e:
        raise PgQuotaError(e.user_message(fallback=str(e))) from e
    except Exception as e:
        raise PgQuotaError(str(e) or "خواندن سقف پاسارگارد ناموفق بود") from e


def _admin_username(admin: Mapping[str, Any] | None) -> str:
    if not isinstance(admin, dict):
        return ""
    return str(admin.get("username") or "").strip()


async def _load_admin_and_role(
    staff: dict,
    *,
    session: Any | None = None,
    client: Any | None = None,
) -> tuple[dict, dict | None]:
    expected = str(staff.get("pg_admin_username") or "").strip()
    if not expected:
        raise PgQuotaError("ادمین پاسارگارد برای این حساب تنظیم نشده است")

    pg = await _quota_pg_client(staff, session=session, client=client)
    admin = None
    try:
        current = await pg.get_current_admin()
    except Exception:
        current = None
    if isinstance(current, dict) and current:
        got = _admin_username(current)
        if got and got.lower() != expected.lower():
            raise PgQuotaError(
                f"سقف پاسارگارد باید با حساب «{expected}» خوانده شود، نه «{got}»"
            )
        if got and got.lower() == expected.lower():
            admin = current
    if not isinstance(admin, dict) or not admin:
        try:
            fetched_admin = await pg.get_admin(expected)
        except Exception:
            fetched_admin = None
        if isinstance(fetched_admin, dict) and fetched_admin:
            got = _admin_username(fetched_admin)
            if got and got.lower() != expected.lower():
                raise PgQuotaError(
                    f"سقف پاسارگارد باید با حساب «{expected}» خوانده شود، نه «{got}»"
                )
            if got and got.lower() == expected.lower():
                admin = fetched_admin
    if not isinstance(admin, dict) or not admin:
        raise PgQuotaError(f"ادمین «{expected}» در پاسارگارد یافت نشد")

    role: dict | None = admin.get("role") if isinstance(admin.get("role"), dict) else None
    role_id = staff.get("pg_role_id")
    if role is None and admin.get("role_id") is not None:
        role_id = role_id or admin.get("role_id")
    if (role is None or not role.get("limits")) and role_id:
        try:
            fetched = await pg.get_admin_role(int(role_id))
            if isinstance(fetched, dict):
                role = fetched
        except Exception:
            pass
    return admin, role


async def load_staff_limit_snapshot(
    staff: dict,
    *,
    session: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return the actor's effective live PasarGuard limits for UI/policy use."""
    empty = {
        "restricted": False,
        "admin": None,
        "role": None,
        "limits": {},
        "max_users": None,
        "current_users": None,
        "remaining_users": None,
        "account_data_limit": None,
        "account_used_traffic": None,
        "account_remaining_traffic": None,
        "per_user_data_min": None,
        "per_user_data_max": None,
        "per_user_expire_min": None,
        "per_user_expire_max": None,
        "hwid_min": None,
        "hwid_max": None,
    }
    if not staff_needs_quota_check(staff):
        return empty

    # UI path: never 500 the page when PG is unreachable / unset.
    # Mutating asserts still fail closed via _load_admin_and_role.
    try:
        admin, role = await _load_admin_and_role(staff, session=session, client=client)
    except PgQuotaError as exc:
        out = dict(empty)
        out["error"] = exc.message
        return out
    limits = merge_role_limits(admin, role)
    max_users = _as_int(limits.get("max_users")) or _as_int(admin.get("max_users"))
    current_users = (
        _as_int(admin.get("total_users"))
        or _as_int(admin.get("users_count"))
        or 0
    )
    account_data_limit = _as_int(admin.get("data_limit")) or _as_int(
        limits.get("data_limit")
    )
    used_traffic = _as_int(admin.get("used_traffic")) or _as_int(
        admin.get("traffic_used")
    ) or 0
    remaining_users = (
        max(0, int(max_users) - int(current_users))
        if max_users is not None and max_users > 0
        else None
    )
    remaining_traffic = (
        max(0, int(account_data_limit) - int(used_traffic))
        if account_data_limit is not None and account_data_limit > 0
        else None
    )
    hmin, hmax = hwid_bounds(limits)
    return {
        "restricted": True,
        "admin": admin,
        "role": role,
        "limits": limits,
        "max_users": max_users,
        "current_users": current_users,
        "remaining_users": remaining_users,
        "account_data_limit": account_data_limit,
        "account_used_traffic": used_traffic,
        "account_remaining_traffic": remaining_traffic,
        "per_user_data_min": _as_int(limits.get("data_limit_min")),
        "per_user_data_max": _as_int(limits.get("data_limit_max")),
        "per_user_expire_min": _as_int(limits.get("expire_min")),
        "per_user_expire_max": _as_int(limits.get("expire_max")),
        "hwid_min": hmin,
        "hwid_max": hmax,
    }


def limit_snapshot_cards(snapshot: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """UI cards for a live PG limit snapshot. Empty when unrestricted."""
    from app.services.formatting import format_bytes

    snap = snapshot or {}
    if not snap.get("restricted"):
        return []
    cards: list[dict[str, str]] = []

    max_users = snap.get("max_users")
    current = snap.get("current_users")
    if max_users:
        used = f"{int(current)} از " if current is not None else ""
        remain = snap.get("remaining_users")
        caption = f"باقی‌مانده {int(remain)}" if remain is not None else ""
        cards.append(
            {
                "key": "users",
                "label": "سقف کاربران",
                "value": f"{used}{int(max_users)}",
                "caption": caption,
            }
        )

    acct = snap.get("account_data_limit")
    if acct:
        used_t = snap.get("account_used_traffic") or 0
        remain_t = snap.get("account_remaining_traffic")
        caption = f"باقی‌مانده {format_bytes(remain_t)}" if remain_t is not None else ""
        cards.append(
            {
                "key": "traffic",
                "label": "سقف حجم حساب",
                "value": f"{format_bytes(used_t)} از {format_bytes(acct)}",
                "caption": caption,
            }
        )

    per_data = snap.get("per_user_data_max")
    if per_data:
        cards.append(
            {
                "key": "per_user_data",
                "label": "حداکثر حجم هر کاربر",
                "value": format_bytes(per_data),
                "caption": "",
            }
        )
    per_exp = snap.get("per_user_expire_max")
    if per_exp:
        days = max(1, int(per_exp) // 86400)
        cards.append(
            {
                "key": "per_user_expire",
                "label": "حداکثر مدت هر کاربر",
                "value": f"{days} روز",
                "caption": "",
            }
        )
    return cards


async def assert_user_plan_within_limits(
    staff: dict,
    *,
    data_limit: int | None,
    duration_days: int | None,
    label: str = "پلن",
    session: Any | None = None,
    client: Any | None = None,
) -> None:
    """Reject plan definitions that exceed the live PG per-user limits."""
    if not staff_needs_quota_check(staff):
        return
    admin, role = await _load_admin_and_role(staff, session=session, client=client)
    assert_admin_can_write(admin, role)
    limits = merge_role_limits(admin, role)
    expire_ts = None
    days = int(duration_days or 0)
    if days > 0:
        expire_ts = int(datetime.now(timezone.utc).timestamp()) + days * 86400
    try:
        _check_data_limit_bounds(limits, data_limit, require_finite=True)
        _check_expire_bounds(limits, expire_ts, require_finite=True)
    except PgQuotaError as exc:
        raise PgQuotaError(f"{label}: {exc.message}") from exc


async def assert_custom_plan_range_within_limits(
    staff: dict,
    *,
    min_gb: float,
    max_gb: float,
    min_days: int,
    max_days: int,
    label: str = "پلن دلخواه",
    session: Any | None = None,
    client: Any | None = None,
) -> None:
    """Validate custom-plan range settings against the live PG per-user limits."""
    if max_gb < min_gb:
        raise PgQuotaError(f"{label}: حداکثر حجم نمی‌تواند کمتر از حداقل حجم باشد")
    if max_days < min_days:
        raise PgQuotaError(f"{label}: حداکثر مدت نمی‌تواند کمتر از حداقل مدت باشد")

    def _bytes(gb: float) -> int:
        return int(float(gb) * (1024**3))

    await assert_user_plan_within_limits(
        staff,
        data_limit=_bytes(min_gb),
        duration_days=int(min_days),
        label=f"{label} (حداقل)",
        session=session,
        client=client,
    )
    await assert_user_plan_within_limits(
        staff,
        data_limit=_bytes(max_gb),
        duration_days=int(max_days),
        label=f"{label} (حداکثر)",
        session=session,
        client=client,
    )


def staff_needs_quota_check(staff: dict) -> bool:
    """Full platform Owner (true PasarGuard sudo) acts as sudo — no PG capacity gate.

    A *Hybrid* Owner — ``role="admin"`` whose ``.env`` PasarGuard account is
    itself a limited admin (``pg_is_owner`` explicitly ``False``, set by
    ``pg_access.enrich_platform_admin_staff``) — must be checked exactly like
    any other restricted admin. Skipping the check for every ``role="admin"``
    used to mean a Hybrid Owner would only discover their own max_users /
    data-cap by PasarGuard's raw rejection instead of the same friendly
    Persian message resellers/pg_staff already get.

    Any non-admin staff must be checked — even when ``pg_admin_username`` is
    missing (that case fails closed in ``_load_admin_and_role``). Missing/
    unset ``pg_is_owner`` (legacy/unenriched staff dict, or a genuine
    non-Hybrid single-owner deployment) still defaults to sudo, preserving
    prior behavior for the common case.
    """
    if staff.get("role") != "admin":
        return True
    return staff.get("pg_is_owner") is False


async def _reserved_user_slots(session: Any | None, staff: dict) -> int:
    if session is None or not integration_capacity_retained():
        return 0
    return await durable_user_slots(session, shop_id=shop_owner_id(staff), pg_username=staff.get("pg_admin_username"))


async def assert_can_create_user(
    staff: dict,
    *,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    from_template: bool = False,
    quantity: int = 1,
    session: Any | None = None,
    client: Any | None = None,
) -> None:
    """Enforce quotas before creating a user that will be owned by this staff."""
    if not staff_needs_quota_check(staff):
        return

    admin, role = await _load_admin_and_role(staff, session=session, client=client)
    assert_admin_can_write(admin, role)
    limits = merge_role_limits(admin, role)
    reserved_slots = await _reserved_user_slots(session, staff)
    _check_max_users(admin, limits, need=max(1, int(quantity or 1)) + reserved_slots)

    if from_template:
        # PasarGuard: template create only checks max_users (+ write gate).
        # HWID/volume come from the template; PG enforces when authenticated as admin.
        return

    _check_data_limit_bounds(limits, data_limit, require_finite=True)
    _check_expire_bounds(limits, expire_ts, require_finite=True)
    # When the form omits HWID but the role caps devices, default to the role max
    # so existing UIs do not accidentally create unlimited-device users.
    hmin, hmax = hwid_bounds(limits)
    effective_hwid = hwid_limit
    if (effective_hwid is None or effective_hwid <= 0) and hmax is not None and hmax > 0:
        effective_hwid = hmax
    elif (effective_hwid is None or effective_hwid <= 0) and hmin is not None and hmin > 0:
        effective_hwid = hmin
    _check_hwid_bounds(limits, effective_hwid, require_finite=True)


async def assert_can_modify_user(
    staff: dict,
    *,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    data_limit_changed: bool = True,
    expire_changed: bool = True,
    hwid_changed: bool = True,
    session: Any | None = None,
    client: Any | None = None,
) -> None:
    """Enforce per-user volume/time/HWID bounds before modify (no max_users check)."""
    if not staff_needs_quota_check(staff):
        return

    admin, role = await _load_admin_and_role(staff, session=session, client=client)
    assert_admin_can_write(admin, role)
    limits = merge_role_limits(admin, role)

    _check_data_limit_bounds(
        limits,
        data_limit,
        require_finite=data_limit_changed,
    )
    _check_expire_bounds(
        limits,
        expire_ts,
        require_finite=expire_changed,
    )
    if hwid_changed:
        _check_hwid_bounds(limits, hwid_limit, require_finite=True)


async def assert_can_mutate_owned_users(
    staff: dict,
    *,
    session: Any | None = None,
    client: Any | None = None,
) -> None:
    """Block enable/disable/reset/revoke/delete when admin is limited/disabled."""
    if not staff_needs_quota_check(staff):
        return
    admin, role = await _load_admin_and_role(staff, session=session, client=client)
    assert_admin_can_write(admin, role)


async def assert_reseller_can_deliver(
    *,
    pg_admin_username: str | None,
    pg_role_id: int | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    from_template: bool = False,
    quantity: int = 1,
    session: Any | None = None,
    reseller_user_id: int | None = None,
    client: Any | None = None,
) -> None:
    """Quota check for shop delivery assigned to a reseller PG admin.

    Fail closed when the shop has no PG admin link — otherwise create-as-owner
    would bypass every role quota. Uses the shop's own PG client, never Owner.
    """
    uname = str(pg_admin_username or "").strip()
    if not uname:
        raise PgQuotaError(
            "ادمین پاسارگارد برای این فروشگاه تنظیم نشده است — تحویل ممکن نیست"
        )
    staff = {
        "role": "reseller",
        "pg_admin_username": uname,
        "pg_role_id": pg_role_id,
    }
    if reseller_user_id:
        staff["bot_user_id"] = int(reseller_user_id)
    own_client = client
    if own_client is None and (session is not None or reseller_user_id):
        own_client = await _client_for_named_pg_admin(
            session=session,
            username=uname,
            reseller_user_id=reseller_user_id,
        )
    await assert_can_create_user(
        staff,
        data_limit=data_limit,
        expire_ts=expire_ts,
        hwid_limit=hwid_limit,
        from_template=from_template,
        quantity=quantity,
        session=session,
        client=own_client,
    )


async def assert_reseller_can_renew(
    *,
    pg_admin_username: str | None,
    pg_role_id: int | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    from_template: bool = False,
    session: Any | None = None,
    reseller_user_id: int | None = None,
    client: Any | None = None,
) -> None:
    """Quota check for shop renewal modifying a reseller-owned user."""
    uname = str(pg_admin_username or "").strip()
    if not uname:
        raise PgQuotaError(
            "ادمین پاسارگارد برای این فروشگاه تنظیم نشده است — تمدید ممکن نیست"
        )
    staff = {
        "role": "reseller",
        "pg_admin_username": uname,
        "pg_role_id": pg_role_id,
    }
    if reseller_user_id:
        staff["bot_user_id"] = int(reseller_user_id)
    own_client = client
    if own_client is None and (session is not None or reseller_user_id):
        own_client = await _client_for_named_pg_admin(
            session=session,
            username=uname,
            reseller_user_id=reseller_user_id,
        )
    if from_template:
        await assert_can_mutate_owned_users(staff, session=session, client=own_client)
        return
    await assert_can_modify_user(
        staff,
        data_limit=data_limit,
        expire_ts=expire_ts,
        hwid_limit=hwid_limit,
        data_limit_changed=data_limit is not None,
        expire_changed=expire_ts is not None,
        hwid_changed=hwid_limit is not None,
        session=session,
        client=own_client,
    )
