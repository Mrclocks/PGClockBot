"""Tiny TTL cache for PasarGuard GET reads — keyed by client identity."""

from __future__ import annotations

import copy
import time
from typing import Any

_TTL_SEC = 4.0
# Callers may accept older data for values that change rarely and are only
# displayed. Mutations through the same client still invalidate at once.
MAX_ACCEPTED_AGE_SEC = 300.0
DASHBOARD_COUNT_MAX_AGE_SEC = 120.0
SYSTEM_SUMMARY_MAX_AGE_SEC = 60.0
OWNED_USER_STATS_MAX_AGE_SEC = 60.0
NODE_LIST_MAX_AGE_SEC = 15.0
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
        if age > MAX_ACCEPTED_AGE_SEC:
            _STORE.pop(k, None)
        return None
    return copy.deepcopy(val)


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
