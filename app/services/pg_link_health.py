"""How the link to PasarGuard behaves: failures by kind and response times.

Kept in memory for the running process (one panel, one origin). It answers
"is it slow, and why?" after a change: timeouts, refused connections, DNS,
dropped connections, reverse-proxy errors or PasarGuard's own 5xx. Paths
and messages are never stored; subscription paths carry customer tokens.
"""

from __future__ import annotations

import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

WINDOW_SEC = 3600.0
# Admins read clock times in Tehran time.
DISPLAY_ZONE = ZoneInfo("Asia/Tehran")
MAX_EVENTS = 2000
MAX_LATENCIES = 500

KIND_LABELS = {
    "timeout": "پاسخ نداد (timeout)",
    "connect": "اتصال برقرار نشد",
    "dns": "خطای DNS",
    "dropped": "اتصال وسط کار قطع شد",
    "gateway": "خطای پراکسی (502/503/504)",
    "server_error": "خطای خود پاسارگارد (5xx)",
    "auth": "ورود به پاسارگارد رد شد (نام کاربری/رمز یا API key)",
    "client_error": "پاسخ نامعتبر از پاسارگارد (4xx)",
}
_DNS_HINTS = ("name or service not known", "temporary failure in name resolution", "getaddrinfo", "nodename nor servname")


@dataclass(slots=True)
class _LinkStats:
    started_at: float = field(default_factory=time.time)
    events: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    latencies: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=MAX_LATENCIES))
    totals: Counter[str] = field(default_factory=Counter)
    reads: int = 0
    last_ok_at: float | None = None
    last_failure_at: float | None = None
    last_failure_kind: str | None = None


_stats = _LinkStats()


def reset() -> None:
    global _stats
    _stats = _LinkStats()


def failure_kind(exc: BaseException | None = None, status_code: int | None = None) -> str:
    """Name the cause of a failed read."""
    if status_code is not None:
        if status_code in (502, 503, 504):
            return "gateway"
        if status_code in (401, 403):
            return "auth"
        if 400 <= status_code < 500:
            return "client_error"
        return "server_error"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        return "dns" if any(hint in text for hint in _DNS_HINTS) else "connect"
    return "dropped"


def record_success(elapsed_sec: float) -> None:
    now = time.time()
    _stats.reads += 1
    _stats.last_ok_at = now
    _stats.latencies.append((now, max(0.0, elapsed_sec) * 1000))


def record_failure(kind: str) -> None:
    if kind not in KIND_LABELS:
        kind = "dropped"
    now = time.time()
    _stats.reads += 1
    _stats.events.append((now, kind))
    _stats.totals[kind] += 1
    _stats.last_failure_at = now
    _stats.last_failure_kind = kind


def latest_failure_kind() -> str | None:
    """Most recent recorded failure kind key (not the Persian label)."""
    return _stats.last_failure_kind


def recent_failures(window_sec: float = WINDOW_SEC) -> Counter[str]:
    cutoff = time.time() - window_sec
    return Counter(kind for moment, kind in _stats.events if moment >= cutoff)


def _percentile(values: list[float], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank percentile.
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index])


def _local_time(moment: float | None) -> str | None:
    return None if moment is None else datetime.fromtimestamp(moment, DISPLAY_ZONE).strftime("%H:%M:%S")


def _iso(moment: float | None) -> str | None:
    return None if moment is None else datetime.fromtimestamp(moment, timezone.utc).isoformat()


def snapshot(circuit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Display data for the owner's connection card."""
    cutoff = time.time() - WINDOW_SEC
    recent_ms = [ms for moment, ms in _stats.latencies if moment >= cutoff]
    recent = recent_failures()
    return {
        "since": _iso(_stats.started_at),
        "reads": _stats.reads,
        "last_ok_at": _iso(_stats.last_ok_at),
        "last_failure_at": _iso(_stats.last_failure_at),
        "last_ok_time": _local_time(_stats.last_ok_at),
        "last_failure_time": _local_time(_stats.last_failure_at),
        "last_failure": KIND_LABELS.get(_stats.last_failure_kind or "", None),
        "latency_p50_ms": _percentile(recent_ms, 0.5),
        "latency_p95_ms": _percentile(recent_ms, 0.95),
        "recent_failures": [
            {"kind": kind, "label": KIND_LABELS[kind], "count": recent[kind], "total": _stats.totals[kind]}
            for kind in KIND_LABELS
            if recent[kind] or _stats.totals[kind]
        ],
        "recent_failure_count": sum(recent.values()),
        "circuit": circuit or {"paused": False, "retry_in_sec": 0, "pause_count": 0},
    }


def summary_text(window_sec: float = 300.0) -> str:
    """Short ``kind:count`` list for log lines."""
    counts = recent_failures(window_sec)
    return ",".join(f"{kind}:{counts[kind]}" for kind in KIND_LABELS if counts[kind]) or "none"
