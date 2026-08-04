"""Build reseller-facing PasarGuard overview metrics (no server/hardware stats)."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.formatting import format_bytes, format_number
from app.services.pasarguard import get_pg


_SERVER_STAT_KEYS = {
    "version",
    "started_at",
    "uptime",
    "uptime_seconds",
    "system_uptime",
    "mem_total",
    "mem_used",
    "mem_free",
    "memory_total",
    "memory_used",
    "memory_free",
    "disk_total",
    "disk_used",
    "disk_free",
    "cpu_usage",
    "cpu_cores",
    "cpu",
    "incoming_bandwidth",
    "outgoing_bandwidth",
    "incoming_bandwidth_speed",
    "outgoing_bandwidth_speed",
    "speed",
    "load",
    "load_avg",
}


def is_server_stat_key(key: str) -> bool:
    k = (key or "").strip().lower()
    if k in _SERVER_STAT_KEYS:
        return True
    hints = ("mem_", "memory", "disk", "cpu", "uptime", "bandwidth", "load_avg", "ram")
    return any(h in k for h in hints)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _role_limits(admin: dict | None, role: dict | None) -> dict:
    from app.services.pg_quota import merge_role_limits

    return merge_role_limits(admin, role)


def remain_tone(remain_pct: float | None) -> str:
    """Tone from remaining capacity: <10% err, <25% caution, <50% warn, else ok."""
    if remain_pct is None:
        return "neutral"
    if remain_pct < 10:
        return "err"
    if remain_pct < 25:
        return "caution"
    if remain_pct < 50:
        return "warn"
    return "ok"


def _meter(
    *,
    label: str,
    used: int | None,
    limit: int | None,
    used_label: str,
    remain_label: str,
    format_value,
    kind: str = "count",
) -> dict[str, Any]:
    from app.services.formatting import format_bytes_ratio, format_count_ratio

    has_limit = limit is not None and int(limit) > 0
    used_v = int(used or 0)
    remain = None
    pct = None
    remain_pct = None
    if has_limit:
        lim = int(limit)
        remain = max(0, lim - used_v)
        pct = min(100.0, (used_v / lim) * 100.0) if lim else 0.0
        remain_pct = max(0.0, 100.0 - pct)
        if kind == "bytes":
            ratio_text = format_bytes_ratio(used_v, lim)
        else:
            ratio_text = format_count_ratio(used_v, lim)
    else:
        ratio_text = format_value(used_v)
    tone = remain_tone(remain_pct)
    exhausted = bool(has_limit and remain is not None and int(remain) <= 0)
    return {
        "label": label,
        "has_limit": has_limit,
        "used": used_v,
        "limit": int(limit) if has_limit else None,
        "remain": remain,
        "pct": pct,
        "remain_pct": remain_pct,
        "tone": tone,
        "exhausted": exhausted,
        "alert": exhausted,  # legacy alias — only when fully depleted
        "used_text": format_value(used_v),
        "limit_text": format_value(limit) if has_limit else "نامحدود",
        "remain_text": format_value(remain) if remain is not None else "—",
        "ratio_text": ratio_text,
        "used_label": used_label,
        "remain_label": remain_label,
    }


def _format_duration(seconds: float | int | None) -> str:
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


def _time_meter(expire_raw: Any, created_raw: Any = None) -> dict[str, Any] | None:
    """Legacy helper kept for tests; admin-account time is not shown in UI."""
    from datetime import datetime, timezone

    from app.services.formatting import format_expire, parse_expire

    exp = parse_expire(expire_raw)
    if not exp:
        return None
    now = datetime.now(timezone.utc)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    remain_sec = max(0.0, (exp - now).total_seconds())
    created = parse_expire(created_raw) if created_raw is not None else None
    used_sec = None
    pct = None
    total_sec = None
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        total_sec = max(1.0, (exp - created).total_seconds())
        used_sec = max(0.0, min(total_sec, (now - created).total_seconds()))
        pct = min(100.0, (used_sec / total_sec) * 100.0)
        remain_sec = max(0.0, total_sec - used_sec)
    expire_text = format_expire(expire_raw)
    return {
        "label": "زمان",
        "has_limit": True,
        "expire_text": expire_text,
        "total_sec": total_sec,
        "total_text": _format_duration(total_sec) if total_sec is not None else expire_text,
        "remain_sec": remain_sec,
        "remain_text": _format_duration(remain_sec) if remain_sec > 0 else "منقضی",
        "used_sec": used_sec,
        "used_text": _format_duration(used_sec) if used_sec is not None else "—",
        "pct": pct,
        "used_label": "مصرف‌شده",
        "remain_label": "باقی‌مانده",
    }


_STATUS_LABELS = {
    "active": ("فعال", "active"),
    "limited": ("محدود", "warn"),
    "disabled": ("غیرفعال", "danger"),
    "expired": ("منقضی", "danger"),
    "on_hold": ("معلق", "warn"),
}


def _status_meta(raw: Any) -> tuple[str | None, str | None]:
    if raw is None or raw == "":
        return None, None
    key = str(raw).strip().lower()
    if key in _STATUS_LABELS:
        return _STATUS_LABELS[key]
    return str(raw), "neutral"


def _constraint_box(label: str, value_text: str) -> dict[str, Any]:
    """Constraint stat box — no caption; only users/volume boxes keep remain hints."""
    return {
        "label": label,
        "value_text": value_text,
    }


def _role_constraint_boxes(limits: dict) -> list[dict[str, Any]]:
    """Per-user role bounds shown as equal-sized stat boxes (no admin-account time)."""
    boxes: list[dict[str, Any]] = []
    dmin = _as_int(limits.get("data_limit_min"))
    dmax = _as_int(limits.get("data_limit_max"))
    emin = _as_int(limits.get("expire_min"))
    emax = _as_int(limits.get("expire_max"))
    hmin = _as_int(limits.get("min_hwid_per_user"))
    hmax = _as_int(limits.get("max_hwid_per_user"))

    if dmin is not None and dmin > 0:
        boxes.append(_constraint_box("حداقل حجم کاربر", format_bytes(dmin)))
    if dmax is not None and dmax > 0:
        boxes.append(_constraint_box("حداکثر حجم کاربر", format_bytes(dmax)))
    if emin is not None and emin > 0:
        boxes.append(_constraint_box("حداقل مدت کاربر", _format_duration(emin)))
    if emax is not None and emax > 0:
        boxes.append(_constraint_box("حداکثر مدت کاربر", _format_duration(emax)))
    if hmin is not None and hmin > 0:
        boxes.append(_constraint_box("حداقل HWID", format_number(hmin)))
    if hmax is not None and hmax > 0:
        boxes.append(_constraint_box("حداکثر HWID", format_number(hmax)))
    return boxes


async def build_reseller_pg_overview(
    staff: dict,
    session: Optional[AsyncSession] = None,
) -> dict[str, Any]:
    """Metrics for a staff member's own PG admin account (reseller or pg_staff).

    Uses the staff-scoped PG client when ``session`` is provided — never the
    Owner token for non-admin (avoids dashboard-count vs empty-list mismatch).
    """
    from app.services.pasarguard import PasarGuardError, get_pg, get_pg_for_staff
    from app.services.pg_credentials import PG_CREDENTIAL_MISSING_MSG

    owner = str(staff.get("pg_admin_username") or "").strip()
    out: dict[str, Any] = {
        "username": owner or None,
        "ready": False,
        "error": None,
        "users": None,
        "traffic": None,
        "time": None,  # kept for back-compat; always None (PG has no admin day quota)
        "constraints": [],
        "status": None,
        "status_label": None,
        "status_badge": None,
        "lifetime_text": None,
        "role_name": None,
        "user_stats": None,
    }
    if not owner:
        out["error"] = "ادمین پاسارگارد برای این حساب تنظیم نشده است"
        return out

    try:
        if staff.get("role") == "admin":
            pg = get_pg()
        elif session is None:
            out["error"] = PG_CREDENTIAL_MISSING_MSG
            return out
        else:
            try:
                pg, _as_owner = await get_pg_for_staff(session, staff)
            except PasarGuardError as e:
                out["error"] = e.user_message(fallback=PG_CREDENTIAL_MISSING_MSG)
                return out
        admin = await pg.get_admin(owner)
        if not admin and staff.get("role") != "admin":
            # Limited clients cannot list /api/admins — own profile is enough.
            admin = await pg.get_current_admin()
        if not admin:
            out["error"] = f"ادمین «{owner}» در پاسارگارد یافت نشد"
            return out

        role_id = staff.get("pg_role_id")
        # Prefer embedded role on admin payload
        role = admin.get("role") if isinstance(admin.get("role"), dict) else None
        if not role and role_id:
            try:
                role = await pg.get_admin_role(int(role_id))
            except Exception:
                role = None

        limits = _role_limits(admin, role)
        total_users = _as_int(admin.get("total_users")) or _as_int(admin.get("users_count")) or 0
        max_users = (
            _as_int(limits.get("max_users"))
            or _as_int(limits.get("users_max"))
            or _as_int(admin.get("max_users"))
        )
        used_traffic = (
            _as_int(admin.get("used_traffic"))
            or _as_int(admin.get("traffic_used"))
            or 0
        )
        data_limit = (
            _as_int(admin.get("data_limit"))
            or _as_int(limits.get("data_limit"))
            or _as_int(limits.get("max_traffic"))
            or _as_int(limits.get("traffic_limit"))
        )
        lifetime = _as_int(admin.get("lifetime_used_traffic"))

        # Surface role name for UI
        role_name = None
        if isinstance(role, dict):
            role_name = role.get("name") or role.get("title")
        elif isinstance(admin.get("role"), dict):
            role_name = admin["role"].get("name") or admin["role"].get("title")
        out["role_name"] = role_name

        out["ready"] = True
        status_raw = admin.get("status") or ("limited" if admin.get("is_limited") else None)
        out["status"] = status_raw
        label, badge = _status_meta(status_raw)
        out["status_label"] = label
        out["status_badge"] = badge
        out["users"] = _meter(
            label="کاربران",
            used=total_users,
            limit=max_users,
            used_label="مصرف‌شده",
            remain_label="باقی‌مانده",
            format_value=lambda v: format_number(v) if v is not None else "—",
            kind="count",
        )
        out["traffic"] = _meter(
            label="حجم",
            used=used_traffic,
            limit=data_limit,
            used_label="مصرف‌شده",
            remain_label="باقی‌مانده",
            format_value=format_bytes,
            kind="bytes",
        )
        if lifetime is not None:
            out["lifetime_text"] = format_bytes(lifetime)

        # Admin-account day/expire is not supported by PasarGuard — omit time meter.
        out["time"] = None
        out["constraints"] = _role_constraint_boxes(limits)
        out["user_stats"] = await _owned_user_stats(pg, owner, fallback_total=total_users)
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


def _owner_username_of(user: dict) -> str:
    admin = user.get("admin") or user.get("owner_username") or ""
    if isinstance(admin, dict):
        admin = admin.get("username") or ""
    return str(admin or "").strip().lower()


def _is_online(user: dict, *, window_sec: int = 120) -> bool:
    """True when online_at is within the recent window (default 2 minutes)."""
    from datetime import datetime, timezone

    from app.services.formatting import parse_expire

    raw = user.get("online_at") or user.get("last_online_at")
    if not raw:
        return False
    dt = parse_expire(raw)
    if not dt:
        # Some APIs return epoch seconds
        try:
            ts = float(raw)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return False
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() <= window_sec


async def _owned_user_stats(pg, owner: str, *, fallback_total: int = 0) -> dict[str, Any]:
    """Count status/online for users owned by this PG admin only — never system-wide."""
    mine = (owner or "").strip().lower()
    stats = {
        "total": int(fallback_total or 0),
        "online": 0,
        "active": 0,
        "limited": 0,
        "expired": 0,
        "disabled": 0,
        "on_hold": 0,
        "other": 0,
        "sampled": False,
        "sample_cap": 500,
    }
    if not mine:
        return stats

    users: list[dict] = []
    try:
        # Prefer server-side admin filter when supported by PasarGuard.
        data = await pg.get_users(admin=owner, limit=stats["sample_cap"], offset=0)
        raw = data.get("users") if isinstance(data, dict) else data
        if isinstance(raw, list):
            users = [u for u in raw if isinstance(u, dict)]
    except Exception:
        users = []

    if not users:
        try:
            data = await pg.get_users(limit=stats["sample_cap"], offset=0)
            raw = data.get("users") if isinstance(data, dict) else data
            if isinstance(raw, list):
                users = [u for u in raw if isinstance(u, dict)]
        except Exception:
            users = []

    # Always filter client-side — never trust unscoped payloads.
    users = [u for u in users if _owner_username_of(u) == mine]

    if not users:
        return stats

    if len(users) >= stats["sample_cap"]:
        stats["sampled"] = True
    else:
        # Exact total from the owned list when we got the full set
        stats["total"] = len(users)

    for u in users:
        st = str(u.get("status") or "").strip().lower()
        if st in ("active", "limited", "expired", "disabled", "on_hold"):
            stats[st] = int(stats.get(st) or 0) + 1
        elif st:
            stats["other"] = int(stats.get("other") or 0) + 1
        if _is_online(u):
            stats["online"] += 1

    # Prefer admin.total_users when list was capped
    if stats["sampled"] and fallback_total:
        stats["total"] = int(fallback_total)

    return stats
