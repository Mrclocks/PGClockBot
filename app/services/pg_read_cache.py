"""Tiny TTL cache for PasarGuard GET reads — keyed by client identity."""

from __future__ import annotations

import copy
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_TTL_SEC = 4.0
# Callers may accept older data for values that change rarely and are only
# displayed. Mutations through the same client still invalidate at once.
MAX_ACCEPTED_AGE_SEC = 300.0
DASHBOARD_COUNT_MAX_AGE_SEC = 120.0
SYSTEM_SUMMARY_MAX_AGE_SEC = 60.0
OWNED_USER_STATS_MAX_AGE_SEC = 60.0
NODE_LIST_MAX_AGE_SEC = 15.0
# Capability probe (GET /api/admin + role map). Bounded; never shared across identities.
CAPABILITY_PROBE_MAX_AGE_SEC = 180.0
CAPABILITY_PROBE_PATH = "__pg_capability_probe__"
ROLE_FEATURES_PATH = "__pg_role_features__"
# Display-only fallback while PasarGuard is unreachable; never for access checks.
LAST_GOOD_MAX_AGE_SEC = 900.0
_MAX = 256
_STORE: dict[tuple, tuple[float, Any]] = {}


def _key(ident: str, path: str, params: Any) -> tuple:
    if params is None:
        param_key = ""
    elif isinstance(params, dict):
        param_key = repr(tuple(sorted((str(k), str(v)) for k, v in params.items())))
    else:
        param_key = repr(params)
    return (str(ident or ""), str(path or ""), param_key)


def cache_get(ident: str, path: str, params: Any = None, *, max_age: float | None = None) -> Any | None:
    k = _key(ident, path, params)
    hit = _STORE.get(k)
    if not hit:
        return None
    ts, val = hit
    age = time.monotonic() - ts
    allowed = _TTL_SEC if max_age is None else min(max(max_age, 0.0), MAX_ACCEPTED_AGE_SEC)
    if age > allowed:
        # Keep entries another caller may still accept with a longer max_age.
        if age > LAST_GOOD_MAX_AGE_SEC:
            _STORE.pop(k, None)
        return None
    return copy.deepcopy(val)


def cache_get_last_good(ident: str, path: str, params: Any = None) -> tuple[Any, float] | None:
    """Last successful answer of this identity and its age, while the panel is down."""
    hit = _STORE.get(_key(ident, path, params))
    if not hit:
        return None
    ts, val = hit
    age = time.monotonic() - ts
    if age > LAST_GOOD_MAX_AGE_SEC:
        return None
    return copy.deepcopy(val), age


@dataclass(slots=True)
class LastGoodNotice:
    """Age in seconds of the oldest saved answer a page shows instead of live data."""

    age: float | None = None

    @property
    def minutes(self) -> int:
        return max(1, round((self.age or 0) / 60))


_NOTICE: ContextVar[LastGoodNotice | None] = ContextVar("pg_last_good_notice", default=None)


def begin_last_good_notice() -> LastGoodNotice:
    """Fresh notice for one web page; the next page request replaces it."""
    notice = LastGoodNotice()
    _NOTICE.set(notice)
    return notice


def last_good_notice_active() -> bool:
    """Saved data may be shown only where the page can say it is not live."""
    return _NOTICE.get() is not None


def note_last_good_read(age: float) -> None:
    notice = _NOTICE.get()
    if notice is not None:
        notice.age = max(notice.age or 0.0, age)


def cache_put(ident: str, path: str, params: Any, value: Any) -> None:
    if value is None:
        return
    if len(_STORE) >= _MAX:
        oldest = min(_STORE, key=lambda item: _STORE[item][0])
        _STORE.pop(oldest, None)
    _STORE[_key(ident, path, params)] = (time.monotonic(), copy.deepcopy(value))


def invalidate_ident(ident: str) -> None:
    prefix = str(ident or "")
    for k in [k for k in _STORE if k[0] == prefix]:
        _STORE.pop(k, None)


def invalidate_capability_cache(*, ident: str | None = None) -> None:
    """Drop capability / role-feature entries (all identities, or one)."""
    paths = {CAPABILITY_PROBE_PATH, ROLE_FEATURES_PATH}
    if ident is None:
        for key in [key for key in _STORE if key[1] in paths]:
            _STORE.pop(key, None)
        return
    prefix = str(ident or "")
    for key in [key for key in _STORE if key[0] == prefix and key[1] in paths]:
        _STORE.pop(key, None)


def clear_store() -> None:
    """Tests only."""
    _STORE.clear()
