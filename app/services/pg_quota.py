"""Enforce PasarGuard admin role quotas when acting via the owner API token.

PasarGuard itself enforces limits only when the limited admin authenticates.
This web panel (and shop delivery) always calls the API as the sudo/owner
account, then reassigns ownership — which bypasses those checks.

This module mirrors PasarGuard's ``_enforce_user_limits`` + limited-admin
write block so staff/resellers cannot exceed the same role limits here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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


async def _load_admin_and_role(staff: dict) -> tuple[dict, dict | None]:
    owner = str(staff.get("pg_admin_username") or "").strip()
    if not owner:
        raise PgQuotaError("ادمین پاسارگارد برای این حساب تنظیم نشده است")

    from app.services.pasarguard import get_pg

    pg = get_pg()
    admin = await pg.get_admin(owner)
    if not isinstance(admin, dict) or not admin:
        raise PgQuotaError(f"ادمین «{owner}» در پاسارگارد یافت نشد")

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


def staff_needs_quota_check(staff: dict) -> bool:
    """Owner web admin (role=admin) acts as sudo — no PG capacity gate.

    Any non-admin staff must be checked — even when ``pg_admin_username`` is
    missing (that case fails closed in ``_load_admin_and_role``).
    """
    return staff.get("role") != "admin"


async def assert_can_create_user(
    staff: dict,
    *,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    from_template: bool = False,
    quantity: int = 1,
) -> None:
    """Enforce quotas before creating a user that will be owned by this staff."""
    if not staff_needs_quota_check(staff):
        return

    admin, role = await _load_admin_and_role(staff)
    assert_admin_can_write(admin, role)
    limits = merge_role_limits(admin, role)
    _check_max_users(admin, limits, need=max(1, int(quantity or 1)))

    # from_template must NOT skip volume/expire checks — oversized templates
    # were a quota bypass. Callers should pass resolved template/plan limits;
    # when omitted, treat as unlimited and apply require_finite against role max.
    if from_template and data_limit is None and expire_ts is None:
        # Explicit unlimited-by-omission path still hits require_finite below.
        pass
    _check_data_limit_bounds(limits, data_limit, require_finite=True)
    _check_expire_bounds(limits, expire_ts, require_finite=True)


async def assert_can_modify_user(
    staff: dict,
    *,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    data_limit_changed: bool = True,
    expire_changed: bool = True,
) -> None:
    """Enforce per-user volume/time bounds before modify (no max_users check)."""
    if not staff_needs_quota_check(staff):
        return

    admin, role = await _load_admin_and_role(staff)
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


async def assert_can_mutate_owned_users(staff: dict) -> None:
    """Block enable/disable/reset/revoke/delete when admin is limited/disabled."""
    if not staff_needs_quota_check(staff):
        return
    admin, role = await _load_admin_and_role(staff)
    assert_admin_can_write(admin, role)


async def assert_reseller_can_deliver(
    *,
    pg_admin_username: str | None,
    pg_role_id: int | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    from_template: bool = False,
    quantity: int = 1,
) -> None:
    """Quota check for shop delivery assigned to a reseller PG admin.

    Fail closed when the shop has no PG admin link — otherwise create-as-owner
    would bypass every role quota.
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
    await assert_can_create_user(
        staff,
        data_limit=data_limit,
        expire_ts=expire_ts,
        from_template=from_template,
        quantity=quantity,
    )


async def assert_reseller_can_renew(
    *,
    pg_admin_username: str | None,
    pg_role_id: int | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    from_template: bool = False,
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
    if from_template:
        await assert_can_mutate_owned_users(staff)
        return
    await assert_can_modify_user(
        staff,
        data_limit=data_limit,
        expire_ts=expire_ts,
        data_limit_changed=data_limit is not None,
        expire_changed=expire_ts is not None,
    )
