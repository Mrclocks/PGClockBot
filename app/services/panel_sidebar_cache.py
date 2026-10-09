"""Short-lived in-process cache for sidebar unread / inbox / cancel dots."""

from __future__ import annotations

import time
from typing import Any

_TTL_SEC = 20.0
_MAX = 256
# (monotonic_ts, tickets_unread, inbox_alert, cancel_alert)
_STORE: dict[str, tuple[float, int, bool, bool]] = {}


def sidebar_cache_key(staff: dict[str, Any] | None) -> str:
    user = staff or {}
    role = str(user.get("role") or "")
    if role == "admin":
        return "admin"
    if role == "reseller":
        return f"reseller:{int(user.get('bot_user_id') or 0)}"
    if role == "pg_staff":
        return f"pg_staff:{int(user.get('pg_staff_id') or 0)}"
    if role == "principal":
        pid = user.get("org_principal_id") or user.get("principal_id") or 0
        return f"principal:{int(pid)}"
    return f"staff:{role}"


def peek_sidebar_counts(key: str) -> tuple[int, bool, bool] | None:
    hit = _STORE.get(key)
    if not hit:
        return None
    ts, unread, alert, cancel_alert = hit
    if time.monotonic() - ts > _TTL_SEC:
        _STORE.pop(key, None)
        return None
    return int(unread), bool(alert), bool(cancel_alert)


def store_sidebar_counts(
    key: str, unread: int, alert: bool, cancel_alert: bool = False
) -> None:
    if len(_STORE) >= _MAX:
        oldest = min(_STORE, key=lambda k: _STORE[k][0])
        _STORE.pop(oldest, None)
    _STORE[key] = (
        time.monotonic(),
        int(unread or 0),
        bool(alert),
        bool(cancel_alert),
    )


def invalidate_sidebar_counts(key: str | None = None) -> None:
    if key:
        _STORE.pop(key, None)
        return
    _STORE.clear()
