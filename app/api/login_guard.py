"""Panel login IP resolution and brute-force failure tracking.

Extracted from ``app.api.app`` so the panel factory stays focused on routing.
Lockouts are persisted under DATA_DIR so restarts / multi-worker boots share
a lockout window. Every check/update reloads+merges from disk under an
exclusive file lock so concurrent workers cannot clobber each other.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator

from fastapi import Request

from app.config import DATA_DIR, get_settings

# ip -> recent failure timestamps (local cache; disk is source of truth)
_LOGIN_FAILURES: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW_SEC = 15 * 60
_LOGIN_MAX_FAILURES = 8
_LOGIN_LOCK_FILE = DATA_DIR / "login_lockouts.json"
_LOGIN_LOCK_MAX_KEYS = 5000


def _prune_stamps(stamps: list[float], *, now: float | None = None) -> list[float]:
    cutoff = (now if now is not None else time.time()) - _LOGIN_WINDOW_SEC
    return [float(t) for t in stamps if float(t) > cutoff]


def _read_disk_unlocked() -> dict[str, list[float]]:
    """Parse lockout file; caller must hold the file lock when coordinating writes."""
    try:
        if not _LOGIN_LOCK_FILE.is_file():
            return {}
        raw = json.loads(_LOGIN_LOCK_FILE.read_text(encoding="utf-8") or "{}")
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        out: dict[str, list[float]] = {}
        for key, stamps in raw.items():
            if not isinstance(key, str) or not isinstance(stamps, list):
                continue
            kept = [
                float(t)
                for t in stamps
                if isinstance(t, (int, float)) and now - float(t) < _LOGIN_WINDOW_SEC
            ]
            if kept:
                out[key] = kept
        return out
    except Exception:
        logging.getLogger(__name__).debug("login lockout load failed", exc_info=True)
        return {}


def _merge_maps(*maps: dict[str, list[float]]) -> dict[str, list[float]]:
    """Union timestamps per key, prune window, cap key count by recency."""
    now = time.time()
    merged: dict[str, list[float]] = {}
    for m in maps:
        for key, stamps in m.items():
            if not key:
                continue
            bucket = merged.setdefault(key, [])
            bucket.extend(float(t) for t in stamps)
    pruned: dict[str, list[float]] = {}
    for key, stamps in merged.items():
        kept = sorted(set(_prune_stamps(stamps, now=now)))
        if kept:
            pruned[key] = kept
    items = sorted(
        pruned.items(),
        key=lambda kv: max(kv[1]) if kv[1] else 0.0,
        reverse=True,
    )
    return dict(items[:_LOGIN_LOCK_MAX_KEYS])


def _write_disk_unlocked(data: dict[str, list[float]]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _LOGIN_LOCK_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _LOGIN_LOCK_FILE)
        try:
            _LOGIN_LOCK_FILE.chmod(0o600)
        except OSError:
            pass
    except Exception:
        logging.getLogger(__name__).debug("login lockout save failed", exc_info=True)


@contextmanager
def _lock_file() -> Iterator[None]:
    """Exclusive flock around read-modify-write of the lockout file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _LOGIN_LOCK_FILE.with_suffix(".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            # Windows / restricted FS: best-effort without flock
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)


def _sync_memory(data: dict[str, list[float]]) -> None:
    _LOGIN_FAILURES.clear()
    _LOGIN_FAILURES.update(data)


def _login_lock_load() -> None:
    """Best-effort hydrate of in-memory lockouts from disk (startup / tests)."""
    with _lock_file():
        data = _read_disk_unlocked()
        _sync_memory(data)


def _login_lock_save() -> None:
    """Persist current memory merged with disk (used by tests / legacy callers)."""
    with _lock_file():
        data = _merge_maps(_read_disk_unlocked(), dict(_LOGIN_FAILURES))
        _sync_memory(data)
        _write_disk_unlocked(data)


_login_lock_load()


def transport_peer_ip(request: Request) -> str:
    """Raw TCP peer host — never influenced by X-Forwarded-For."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _trust_forwarded_headers(request: Request) -> bool:
    """Phase 3: TRUST_PROXY only applies when the TCP peer is a trusted proxy."""
    try:
        settings = get_settings()
        if not settings.trust_proxy:
            return False
        from app.services.security_policy import peer_is_trusted_proxy

        return peer_is_trusted_proxy(
            transport_peer_ip(request),
            trusted_proxies_raw=getattr(settings, "trusted_proxies", "") or "",
        )
    except Exception:
        return False


def client_ip(request: Request) -> str:
    """Best-effort real client IP, resistant to X-Forwarded-For spoofing.

    X-Forwarded-For is fully attacker-controlled except for the hop(s) your
    own trusted reverse proxy appends. Phase 3: headers are ignored unless
    ``TRUST_PROXY=1`` **and** the TCP peer is in ``TRUSTED_PROXIES``
    (default: loopback only). When trusted, we read the entry counted from
    the RIGHT by ``TRUST_PROXY_HOPS`` — never the left-most entry.
    """
    try:
        if _trust_forwarded_headers(request):
            settings = get_settings()
            raw = request.headers.get("x-forwarded-for") or ""
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            hops = max(1, int(getattr(settings, "trust_proxy_hops", 1) or 1))
            if len(parts) >= hops:
                candidate = parts[-hops]
                if candidate:
                    return candidate
    except Exception:
        pass
    return transport_peer_ip(request)


def forwarded_proto_is_https(request: Request) -> bool:
    """True when a trusted proxy reports ``X-Forwarded-Proto: https``."""
    if not _trust_forwarded_headers(request):
        return False
    fwd = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return fwd == "https"


def login_blocked(ip: str) -> bool:
    """True if ``ip`` (or ``ip|user`` key) has hit the failure ceiling."""
    with _lock_file():
        data = _merge_maps(_read_disk_unlocked(), dict(_LOGIN_FAILURES))
        stamps = _prune_stamps(data.get(ip, []))
        if stamps:
            data[ip] = stamps
        elif ip in data:
            del data[ip]
        _sync_memory(data)
        return len(stamps) >= _LOGIN_MAX_FAILURES


def login_fail(ip: str) -> None:
    with _lock_file():
        data = _merge_maps(_read_disk_unlocked(), dict(_LOGIN_FAILURES))
        stamps = _prune_stamps(data.get(ip, []))
        stamps.append(time.time())
        data[ip] = stamps
        data = _merge_maps(data)  # re-apply key cap
        _sync_memory(data)
        _write_disk_unlocked(data)


def login_success(ip: str) -> None:
    with _lock_file():
        data = _merge_maps(_read_disk_unlocked(), dict(_LOGIN_FAILURES))
        data.pop(ip, None)
        _sync_memory(data)
        _write_disk_unlocked(data)
