from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional
import html as html_mod


def copyable(value: Any, *, empty: str = "—") -> str:
    """Telegram HTML <code> so the user can tap-to-copy usernames, passwords, URLs."""
    if value is None:
        return empty
    text = str(value).strip()
    if not text:
        return empty
    return f"<code>{html_mod.escape(text)}</code>"


def format_user_label(user: Any = None, *, telegram_id: int | None = None) -> str:
    """Prefer @username; fall back to full name, then numeric id."""
    if user is not None:
        uname = (getattr(user, "username", None) or "").strip()
        if uname:
            return f"@{html_mod.escape(uname.lstrip('@'))}"
        name = (getattr(user, "full_name", None) or "").strip()
        if name:
            return html_mod.escape(name)
        tid = getattr(user, "telegram_id", None)
        if tid:
            return str(tid)
    if telegram_id:
        return str(telegram_id)
    return "—"


def bot_user_panel_label(user: Any = None, *, fallback_id: int | None = None) -> str:
    """Plain-text user label for web tables (no HTML)."""
    if user is None:
        return str(fallback_id or "—")
    name = (getattr(user, "full_name", None) or "").strip()
    uname = (getattr(user, "username", None) or "").strip().lstrip("@")
    if name and uname:
        return f"{name} (@{uname})"
    if uname:
        return f"@{uname}"
    if name:
        return name
    tid = getattr(user, "telegram_id", None)
    if tid:
        return str(tid)
    uid = getattr(user, "id", None)
    return str(uid or fallback_id or "—")


def _byte_unit_table() -> tuple[tuple[float, str], ...]:
    """(divisor, Persian label) from smallest to largest."""
    return (
        (1.0, "بایت"),
        (1024.0, "کیلوبایت"),
        (1024.0**2, "مگ"),
        (1024.0**3, "گیگ"),
        (1024.0**4, "ترابایت"),
        (1024.0**5, "پتابایت"),
    )


def _pick_byte_unit(nbytes: float) -> tuple[float, str]:
    n = abs(float(nbytes))
    units = _byte_unit_table()
    chosen = units[0]
    for div, label in units:
        if n >= div:
            chosen = (div, label)
        else:
            break
    return chosen


def _fmt_unit_amount(n: float, *, precision: int | None = None) -> str:
    if precision is not None:
        return f"{n:.{precision}f}"
    if n >= 100:
        return f"{n:.0f}"
    if n >= 10:
        return f"{n:.1f}".rstrip("0").rstrip(".")
    return f"{n:.2f}".rstrip("0").rstrip(".")


def format_bytes(num: int | float | None, *, precision: int | None = None) -> str:
    """Human-readable size using IEC binary units (1024) with Persian labels.

    Logical order is «number unit» (e.g. «10 گیگ»). In the RTL web panel the
    unit renders on the visual left of the number run — do not wrap the whole
    string in dir=ltr.
    """
    amount, unit = format_bytes_parts(num, precision=precision)
    if not unit:
        return amount
    return f"{amount} {unit}"


def format_bytes_parts(
    num: int | float | None, *, precision: int | None = None
) -> tuple[str, str]:
    """Split size into ``(amount, unit)`` for large-number / small-unit UI.

    ``None`` → («نامحدود», «»). Invalid → («—», «»).
    """
    if num is None:
        return "نامحدود", ""
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "—", ""
    if n < 0:
        n = abs(n)
    div, label = _pick_byte_unit(n)
    if div == 1.0:
        return f"{int(round(n))}", label
    return _fmt_unit_amount(n / div, precision=precision), label


def format_bytes_rate(num: int | float | None, *, precision: int | None = None) -> str:
    """Throughput label: «۱۰ مگ/ثانیه». Missing/invalid → «—» (not «نامحدود»)."""
    amount, unit = format_bytes_rate_parts(num, precision=precision)
    if not unit:
        return amount
    return f"{amount} {unit}"


def format_bytes_rate_parts(
    num: int | float | None, *, precision: int | None = None
) -> tuple[str, str]:
    """Split rate into ``(amount, «مگ/ثانیه»)`` for display hierarchy."""
    if num is None:
        return "—", ""
    try:
        float(num)
    except (TypeError, ValueError):
        return "—", ""
    amount, unit = format_bytes_parts(num, precision=precision)
    if amount in {"نامحدود", "—"} or not unit:
        return "—", ""
    return amount, f"{unit}/ثانیه"


def format_bytes_ratio(
    used: int | float | None,
    limit: int | float | None,
    *,
    precision: int | None = None,
    joiner: str = " / ",
) -> str:
    """Shared-unit used/limit string, e.g. «10 / 100 گیگ» or «10 از 100 گیگ».

    Number run first, unit once at the end of the string. Web CSS uses RTL so
    «گیگ» sits on the visual left; never put dir=ltr on the whole cell.
    """
    parts = format_bytes_ratio_parts(used, limit, precision=precision)
    if not parts.get("unit"):
        if parts.get("used") == "—" and parts.get("total") == "—":
            return "—"
        if parts.get("total") in {"", None} and parts.get("used"):
            return str(parts["used"])
        return "—"
    used_s = parts.get("used") or "0"
    total_s = parts.get("total") or "—"
    return f"{used_s}{joiner}{total_s} {parts['unit']}"


def format_bytes_ratio_parts(
    used: int | float | None,
    limit: int | float | None,
    *,
    precision: int | None = None,
) -> dict[str, str]:
    """Split ratio into ``{used, total, unit}`` for num/unit UI hierarchy."""
    empty = {"used": "—", "total": "—", "unit": ""}
    if limit is None:
        amt, unit = format_bytes_parts(used, precision=precision)
        return {"used": amt, "total": "", "unit": unit}
    try:
        u = float(used or 0)
        lim = float(limit)
    except (TypeError, ValueError):
        return empty
    if lim <= 0:
        if u <= 0:
            return {"used": "0", "total": "∞", "unit": "بایت"}
        div, label = _pick_byte_unit(u)
        if div == 1.0:
            return {"used": f"{int(round(u))}", "total": "∞", "unit": label}
        return {
            "used": _fmt_unit_amount(u / div, precision=precision),
            "total": "∞",
            "unit": label,
        }
    if u < 0:
        u = abs(u)
    div, label = _pick_byte_unit(max(u, lim))
    if div == 1.0:
        return {
            "used": f"{int(round(u))}",
            "total": f"{int(round(lim))}",
            "unit": label,
        }
    return {
        "used": _fmt_unit_amount(u / div, precision=precision),
        "total": _fmt_unit_amount(lim / div, precision=precision),
        "unit": label,
    }


def format_count_ratio(used: int | float | None, limit: int | float | None) -> str:
    """Integer used/limit without spaces: «۱۲/۲۰»."""
    if limit is None:
        return format_number(used)
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        return format_number(used)
    if lim <= 0:
        return format_number(used)
    try:
        u = int(used or 0)
    except (TypeError, ValueError):
        u = 0
    return f"{format_number(u)}/{format_number(lim)}"


def format_gb(gb: float | int | None) -> str:
    if gb is None:
        return "نامحدود"
    try:
        n = float(gb)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "نامحدود"
    return format_bytes(n * (1024**3))


def format_number(num: int | float | None) -> str:
    """Format a number for panel templates.

    Must never raise on Jinja ``Undefined`` / bad types — missing template
    attrs used to 500 whole pages (e.g. dashboard funnel stats).
    """
    if num is None:
        return "—"
    try:
        from jinja2.runtime import Undefined

        if isinstance(num, Undefined):
            return "—"
    except Exception:
        pass
    try:
        if isinstance(num, float) and not num.is_integer():
            return f"{num:,.2f}".replace(",", "٬")
        return f"{int(num):,}".replace(",", "٬")
    except (TypeError, ValueError):
        return "—"


def format_money(amount: int | None) -> str:
    """Group web monetary amounts with ASCII commas without changing their value."""
    return format_number(amount).replace("٬", ",")


def format_uptime(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if total < 0:
        total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} روز")
    if hours or days:
        parts.append(f"{hours} ساعت")
    if mins or not parts:
        parts.append(f"{mins} دقیقه")
    if not days and not hours:
        parts.append(f"{secs} ثانیه")
    return " و ".join(parts)


def format_metric(key: str, value: Any) -> str:
    """Pretty-print PasarGuard / system stats by field name."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "بله" if value else "خیر"
    key_l = str(key).lower()

    if "uptime" in key_l and isinstance(value, (int, float)):
        return format_uptime(value)
    if key_l in {"cpu_usage", "cpu"} and isinstance(value, (int, float)):
        return f"{float(value):.1f}٪".replace(".", "٫")

    byte_hints = (
        "traffic",
        "bandwidth",
        "byte",
        "upload",
        "download",
        "memory",
        "mem_",
        "ram",
        "disk",
        "storage",
        "data_limit",
        "used_traffic",
        "lifetime",
    )
    if any(h in key_l for h in byte_hints) and isinstance(value, (int, float)):
        # user counts must stay numeric
        if key_l.endswith("_users") or key_l.endswith("_user") or "count" in key_l:
            return format_number(value)
        return format_bytes(value)
    if isinstance(value, float):
        return format_number(value)
    if isinstance(value, int):
        return format_number(value)
    return str(value)


STAT_LABELS_FA: dict[str, str] = {
    "version": "نسخه پنل",
    "started_at": "شروع سرویس",
    "uptime": "آپ‌تایم",
    "uptime_seconds": "آپ‌تایم",
    "system_uptime": "آپ‌تایم سیستم",
    "mem_total": "کل حافظه",
    "mem_used": "حافظه مصرفی",
    "mem_free": "حافظه آزاد",
    "memory_total": "کل حافظه",
    "memory_used": "حافظه مصرفی",
    "disk_total": "کل دیسک",
    "disk_used": "دیسک مصرفی",
    "disk_free": "دیسک آزاد",
    "cpu_usage": "مصرف پردازنده",
    "cpu_cores": "هسته‌های پردازنده",
    "total_user": "کل کاربران",
    "total_users": "کل کاربران",
    "users_total": "کل کاربران",
    "active_users": "کاربران فعال",
    "users_active": "کاربران فعال",
    "disabled_users": "کاربران غیرفعال",
    "users_disabled": "کاربران غیرفعال",
    "expired_users": "کاربران منقضی",
    "users_expired": "کاربران منقضی",
    "limited_users": "کاربران اتمام‌حجم",
    "users_limited": "کاربران اتمام‌حجم",
    "on_hold_users": "کاربران در انتظار",
    "online_users": "کاربران آنلاین",
    "users_online": "کاربران آنلاین",
    "total_admin": "تعداد ادمین",
    "admins_total": "تعداد ادمین",
    "total_node": "تعداد نود",
    "nodes_total": "تعداد نود",
    "nodes_online": "نودهای آنلاین",
    "incoming_bandwidth": "پهنای باند ورودی",
    "outgoing_bandwidth": "پهنای باند خروجی",
    "incoming_bandwidth_speed": "سرعت ورودی",
    "outgoing_bandwidth_speed": "سرعت خروجی",
    "panel_traffic": "ترافیک پنل",
    "users_active_percentage": "درصد کاربران فعال",
}


def label_stat_key(key: str) -> str:
    k = str(key)
    if k in STAT_LABELS_FA:
        return STAT_LABELS_FA[k]
    low = k.lower()
    if low in STAT_LABELS_FA:
        return STAT_LABELS_FA[low]
    known_bits = {
        "users": "کاربران",
        "user": "کاربر",
        "disabled": "غیرفعال",
        "active": "فعال",
        "expired": "منقضی",
        "limited": "محدود",
        "online": "آنلاین",
        "total": "کل",
        "nodes": "نودها",
        "node": "نود",
        "admins": "ادمین‌ها",
        "admin": "ادمین",
        "traffic": "ترافیک",
        "bandwidth": "پهنای باند",
        "incoming": "ورودی",
        "outgoing": "خروجی",
        "memory": "حافظه",
        "mem": "حافظه",
        "disk": "دیسک",
        "cpu": "پردازنده",
        "cores": "هسته‌ها",
        "usage": "مصرف",
        "version": "نسخه",
        "speed": "سرعت",
        "uptime": "آپ‌تایم",
        "seconds": "",
        "hold": "انتظار",
        "on": "",
        "used": "مصرفی",
        "free": "آزاد",
    }
    parts = [known_bits.get(p, p) for p in low.split("_") if p]
    parts = [p for p in parts if p]
    return " ".join(parts) if parts else k.replace("_", " ")


def format_stat_row(key: str, value: Any) -> tuple[str, str]:
    return label_stat_key(key), format_metric(key, value)


def format_system_stats(stats: dict | Any, *, limit: int = 40) -> str:
    """Full Persian human-readable system stats block for bot/web."""
    if not isinstance(stats, dict):
        return str(stats)
    lines: list[str] = []
    for key, val in list(stats.items())[:limit]:
        if isinstance(val, (dict, list)):
            continue
        label, pretty = format_stat_row(str(key), val)
        lines.append(f"• <b>{label}</b>: {pretty}")
    return "\n".join(lines) if lines else "آماری نیست."


TICKET_STATUS_FA = {
    "open": "باز",
    "answered": "پاسخ‌داده‌شده",
    "closed": "بسته",
}


def ticket_status_fa(status: str | None) -> str:
    if not status:
        return "نامشخص"
    return TICKET_STATUS_FA.get(str(status).lower(), str(status))


ORDER_STATUS_FA = {
    "pending": "در انتظار",
    "awaiting_receipt": "منتظر رسید",
    "awaiting_approval": "منتظر تأیید",
    "paid": "پرداخت‌شده",
    "delivering": "در حال تحویل",
    "delivered": "تحویل‌شده",
    "rejected": "ردشده",
    "cancelled": "لغوشده",
}


def order_status_fa(status: str | None) -> str:
    if not status:
        return "نامشخص"
    return ORDER_STATUS_FA.get(str(status).lower(), str(status))


NODE_STATUS_FA = {
    "connected": "متصل",
    "connecting": "در حال اتصال",
    "error": "خطا",
    "disabled": "غیرفعال",
    "healthy": "سالم",
    "unhealthy": "ناسالم",
    "online": "آنلاین",
    "offline": "آفلاین",
}


def node_status_fa(status: str | None) -> str:
    if not status:
        return "نامشخص"
    return NODE_STATUS_FA.get(str(status).lower(), str(status))


def format_toman(amount: int, currency: str = "تومان") -> str:
    """Persian money string; leading RLM keeps Telegram RTL for numeric lines."""
    return f"\u200f{format_number(amount)} {currency}"


def progress_bar(used: float, total: float | None, width: int = 10) -> str:
    if not total or total <= 0:
        filled = 0
        pct = 0
    else:
        pct = max(0, min(100, int((used / total) * 100)))
        filled = int(round((pct / 100) * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct}%"


def parse_expire(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts <= 0:
            # PG: 0 / negative = unset (unlimited OR on_hold pending start) —
            # never treat as unix epoch 1970.
            return None
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _expire_raw_unset(value: Any) -> bool:
    if value in (None, "", 0, 0.0):
        return True
    if isinstance(value, (int, float)) and int(value) <= 0:
        return True
    return False


def parse_expire_duration_seconds(value: Any) -> int | None:
    """PasarGuard ``expire_duration`` (seconds after first connect) for on_hold."""
    if value in (None, "", 0, 0.0):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n > 0 else None


def is_on_hold_status(status: Any) -> bool:
    s = str(status or "").strip().lower().replace("-", "_")
    return s in {"on_hold", "onhold"}


def on_hold_expire_duration_seconds(info: dict[str, Any] | None) -> int | None:
    """Seconds of validity after first connect when status is on_hold and expire unset."""
    if not isinstance(info, dict) or not is_on_hold_status(info.get("status")):
        return None
    exp_raw = info["expire"] if "expire" in info else info.get("expire_date")
    if not _expire_raw_unset(exp_raw):
        return None
    return hold_duration_from_info(info)


def format_expire_duration_days(seconds: int | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    days = max(1, int((int(seconds) + 86399) // 86400))
    return f"{days} روز"


def format_expire(value: Any, *, status: Any = None, expire_duration: Any = None) -> str:
    dt = parse_expire(value)
    if not dt:
        if is_on_hold_status(status):
            dur = parse_expire_duration_seconds(expire_duration)
            if dur is not None:
                return f"{format_expire_duration_days(dur)} (پس از اتصال)"
            return "پس از اتصال"
        return "نامحدود"
    local = dt.astimezone()
    remaining = dt - datetime.now(timezone.utc)
    days = max(0, remaining.days)
    hours = max(0, remaining.seconds // 3600)
    return f"{local.strftime('%Y/%m/%d %H:%M')} ({days} روز و {hours} ساعت)"


def format_expire_short(
    value: Any, *, status: Any = None, expire_duration: Any = None
) -> str:
    """Compact expire for tables: date + remaining days."""
    dt = parse_expire(value)
    if not dt:
        if is_on_hold_status(status):
            dur = parse_expire_duration_seconds(expire_duration)
            if dur is not None:
                return f"{format_expire_duration_days(dur)} · در انتظار اتصال"
            return "در انتظار اتصال"
        return "—"
    remaining = dt - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return f"{dt.astimezone().strftime('%Y/%m/%d')} · منقضی"
    days = max(1, int((remaining.total_seconds() + 86399) // 86400))
    return f"{dt.astimezone().strftime('%Y/%m/%d')} · {days} روز"


def expire_remaining_days(
    value: Any, *, status: Any = None, expire_duration: Any = None
) -> int | None:
    """Whole days left until expire (ceil); None if unlimited *or* unknown.

    For on_hold with ``expire_duration``, returns the pending duration in days
    (not unlimited). on_hold without duration → None meaning *pending*, not unlimited.
    """
    dt = parse_expire(value)
    if not dt:
        if is_on_hold_status(status):
            dur = parse_expire_duration_seconds(expire_duration)
            if dur is not None:
                return max(1, int((int(dur) + 86399) // 86400))
            return None  # on_hold without duration — unknown, not unlimited
        return None
    remaining = dt - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return 0
    return max(1, int((remaining.total_seconds() + 86399) // 86400))


def hold_duration_from_info(info: Mapping[str, Any] | None) -> int | None:
    """Read PG's pending duration in seconds, with legacy field fallbacks.

    A null/zero/invalid alias must not hide a positive duration from another field.
    """
    if not isinstance(info, Mapping):
        return None
    for key in ("on_hold_expire_duration", "expire_duration", "hold_expire_duration"):
        duration = parse_expire_duration_seconds(info.get(key))
        if duration is not None:
            return duration
    return None


def time_remaining_label(
    *,
    days_left: int | None,
    status: Any = None,
) -> str:
    """UI label for remaining time — never map on_hold + null days to «نامحدود»."""
    if days_left is not None:
        label = f"{int(days_left)} روز"
        return f"{label} (پس از اتصال)" if is_on_hold_status(status) else label
    if is_on_hold_status(status):
        return "پس از اتصال"
    return "نامحدود"


def pg_expire_fields(info: Mapping[str, Any] | dict | None) -> dict[str, Any]:
    """Derive days / text / labels from a PasarGuard user payload (status-aware)."""
    info = info if isinstance(info, dict) else {}
    status = info.get("status")
    expire = info["expire"] if "expire" in info else info.get("expire_date")
    hold_dur = hold_duration_from_info(info)
    days = expire_remaining_days(expire, status=status, expire_duration=hold_dur)
    return {
        "status": status,
        "expire_raw": expire,
        "expire_duration": hold_dur,
        "days_left": days,
        "expire_text": format_expire_short(
            expire, status=status, expire_duration=hold_dur
        ),
        "expire_long": format_expire(expire, status=status, expire_duration=hold_dur),
        "time_label": time_remaining_label(days_left=days, status=status),
        "pending_start": bool(is_on_hold_status(status)),
    }


def data_limit_to_gb(value: Any) -> float | None:
    if value is None or value == 0:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return round(n / (1024**3), 2)


STATUS_FA = {
    "active": "🟢 فعال",
    "disabled": "🔴 غیرفعال",
    "limited": "🟠 اتمام حجم",
    "expired": "⚫ منقضی",
    "on_hold": "🟡 در انتظار اتصال",
}

# Web panel badges — no Telegram emoji dots
STATUS_FA_PLAIN = {
    "active": "فعال",
    "disabled": "غیرفعال",
    "limited": "اتمام حجم",
    "expired": "منقضی",
    "on_hold": "در انتظار اتصال",
}


def status_label(status: str | None) -> str:
    if not status:
        return "نامشخص"
    if is_on_hold_status(status):
        return STATUS_FA["on_hold"]
    return STATUS_FA.get(status.lower(), status)


def status_label_plain(status: str | None) -> str:
    if not status:
        return "نامشخص"
    if is_on_hold_status(status):
        return STATUS_FA_PLAIN["on_hold"]
    return STATUS_FA_PLAIN.get(status.lower(), status)


def kv_line(emoji: str, label: str, value: str) -> str:
    """One labeled row for Telegram HTML cards (RTL-safe)."""
    return f"\u200f{emoji} <b>{label}:</b> {value}"


def info_block(lines: list[str]) -> str:
    """Join info rows with comfortable spacing."""
    clean = [ln.strip() for ln in lines if ln and str(ln).strip()]
    return "\n".join(clean)


def service_card(info: dict, currency_note: str = "") -> str:
    username = info.get("username", "—")
    status_raw = info.get("status")
    status = status_label(status_raw)
    used = info.get("used_traffic")
    limit = info.get("data_limit")
    expire_raw = info.get("expire") if "expire" in info else info.get("expire_date")
    expire = format_expire(
        expire_raw,
        status=status_raw,
        expire_duration=hold_duration_from_info(info),
    )
    if not ("expire" in info or "expire_date" in info or is_on_hold_status(status_raw)):
        expire = "—"
    volume_known = "data_limit" in info and used is not None
    bar = progress_bar(float(used), float(limit) if limit else None) if volume_known else None
    lines = [
        f"👤 {copyable(username)}",
        "",
        kv_line("📶", "وضعیت", status),
        kv_line("📦", "حجم", format_bytes_ratio(used, limit, joiner=" از ") if volume_known else "—"),
        kv_line("📅", "انقضا", f"<b>{expire}</b>"),
    ]
    if bar is not None:
        lines.insert(-1, f"<code>{bar}</code>")
    if info.get("error_message"):
        lines.extend(["", "⚠️ خطا در دریافت اطلاعات سرویس: " + html_mod.escape(str(info["error_message"]))])
        if info.get("error_code"):
            lines.append("کد خطا: " + copyable(info["error_code"]))
    if currency_note:
        lines.extend(["", currency_note])
    online = info.get("online_at")
    if online:
        lines.append(kv_line("⏱", "آخرین آنلاین", format_expire(online)))
    return "\n".join(lines)


def rtl_text(text: str) -> str:
    """Prefix each line with RLM so numeric-leading rows stay right-aligned in Telegram."""
    if not text:
        return text
    return "\n".join(("\u200f" + line) if line else line for line in str(text).split("\n"))


def format_message(title: str, body: str = "") -> str:
    """Pretty Telegram HTML card — bold title, soft divider, spaced body."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not body:
        out = f"<b>{title}</b>" if title else ""
    elif not title:
        out = body
    else:
        out = f"<b>{title}</b>\n━━━━━━━━━━━━\n{body}"
    return rtl_text(out)
