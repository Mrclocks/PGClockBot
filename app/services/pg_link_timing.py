"""Where the time of a PasarGuard request goes: pool wait, connect, TLS and first byte.

Fed by httpx's ``trace`` extension on every panel client, kept in memory with a
fixed size, and shown on the owner's connection card. It answers whether the
link is slow because every request opens a new TCP/TLS connection, because the
panel is slow to answer, or because requests queue for a pooled connection.

Only the HTTP method and a coarse endpoint label are kept. Paths, query
strings, headers and bodies are never stored: subscription paths and usernames
carry customer data.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

WINDOW_SEC = 3600.0
MAX_SAMPLES = 2000
# Endpoints listed on the card, slowest first by p95.
MAX_ENDPOINTS_SHOWN = 5
# Endpoints need a few samples before their p95 means anything.
MIN_ENDPOINT_SAMPLES = 3
OTHER_ENDPOINT = "other"
_RESOURCE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

_CONNECT_STARTED = "connection.connect_tcp.started"
_CONNECT_COMPLETE = "connection.connect_tcp.complete"
_TLS_STARTED = "connection.start_tls.started"
_TLS_COMPLETE = "connection.start_tls.complete"
_SEND_STARTED = "http11.send_request_headers.started"
_HEADERS_COMPLETE = "http11.receive_response_headers.complete"


@dataclass(slots=True, frozen=True)
class TimingSample:
    at: float
    endpoint: str
    pool_ms: float
    connect_ms: float | None
    tls_ms: float | None
    first_byte_ms: float
    new_connection: bool


@dataclass(slots=True)
class _Stats:
    samples: deque[TimingSample] = field(default_factory=lambda: deque(maxlen=MAX_SAMPLES))


_stats = _Stats()


def reset() -> None:
    global _stats
    _stats = _Stats()


def endpoint_label(method: str, path: str) -> str:
    """``GET /api/user/…`` style label: the API resource only, never ids, names or tokens."""
    verb = method.upper() if method.upper() in _METHODS else "OTHER"
    segments = [segment for segment in path.split("/") if segment]
    if "api" not in segments:
        return f"{verb} {OTHER_ENDPOINT}"
    rest = segments[segments.index("api") + 1:]
    if not rest or not _RESOURCE.match(rest[0]):
        return f"{verb} {OTHER_ENDPOINT}"
    suffix = "/…" if len(rest) > 1 else ""
    return f"{verb} /api/{rest[0]}{suffix}"


class _RequestTrace:
    """Collects the phase marks of one request; records a sample once headers arrive."""

    __slots__ = ("_endpoint", "_queued_at", "_marks")

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._queued_at = time.monotonic()
        self._marks: dict[str, float] = {}

    async def __call__(self, event_name: str, info: dict[str, Any]) -> None:
        now = time.monotonic()
        if event_name in (_CONNECT_STARTED, _TLS_STARTED, _SEND_STARTED):
            # Connect retries keep the first start so the wait for a working socket counts in full.
            self._marks.setdefault(event_name, now)
            return
        if event_name in (_CONNECT_COMPLETE, _TLS_COMPLETE):
            self._marks[event_name] = now
            return
        if event_name == _HEADERS_COMPLETE:
            self._marks[event_name] = now
            sample = self._sample()
            if sample is not None:
                _stats.samples.append(sample)

    def _span(self, start: str, end: str) -> float | None:
        if start not in self._marks or end not in self._marks:
            return None
        return max(0.0, self._marks[end] - self._marks[start]) * 1000

    def _sample(self) -> TimingSample | None:
        first_byte = self._span(_SEND_STARTED, _HEADERS_COMPLETE)
        if first_byte is None:
            return None
        opened_at = self._marks.get(_CONNECT_STARTED, self._marks[_SEND_STARTED])
        return TimingSample(
            at=time.time(),
            endpoint=self._endpoint,
            pool_ms=max(0.0, opened_at - self._queued_at) * 1000,
            connect_ms=self._span(_CONNECT_STARTED, _CONNECT_COMPLETE),
            tls_ms=self._span(_TLS_STARTED, _TLS_COMPLETE),
            first_byte_ms=first_byte,
            new_connection=_CONNECT_STARTED in self._marks,
        )


async def attach_trace(request: httpx.Request) -> None:
    """httpx request hook: time this request's phases without keeping its path."""
    request.extensions["trace"] = _RequestTrace(endpoint_label(request.method, request.url.path))


def _percentile(values: list[float], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index])


def _phase(values: list[float]) -> dict[str, int | None]:
    return {"p50_ms": _percentile(values, 0.5), "p95_ms": _percentile(values, 0.95), "count": len(values)}


def _slowest_endpoints(samples: list[TimingSample]) -> list[dict[str, Any]]:
    by_endpoint: dict[str, list[float]] = {}
    for sample in samples:
        by_endpoint.setdefault(sample.endpoint, []).append(sample.first_byte_ms)
    rows = [
        {"endpoint": endpoint, **_phase(values)}
        for endpoint, values in by_endpoint.items()
        if len(values) >= MIN_ENDPOINT_SAMPLES
    ]
    rows.sort(key=lambda row: (row["p95_ms"] or 0, row["count"]), reverse=True)
    return rows[:MAX_ENDPOINTS_SHOWN]


def snapshot(window_sec: float = WINDOW_SEC) -> dict[str, Any]:
    """Phase percentiles, how often a new connection was opened, and the slowest endpoints."""
    cutoff = time.time() - window_sec
    samples = [sample for sample in _stats.samples if sample.at >= cutoff]
    opened = sum(1 for sample in samples if sample.new_connection)
    return {
        "requests": len(samples),
        "new_connections": opened,
        "new_connection_pct": round(100 * opened / len(samples)) if samples else None,
        "pool": _phase([sample.pool_ms for sample in samples]),
        "connect": _phase([sample.connect_ms for sample in samples if sample.connect_ms is not None]),
        "tls": _phase([sample.tls_ms for sample in samples if sample.tls_ms is not None]),
        "first_byte": _phase([sample.first_byte_ms for sample in samples]),
        "endpoints": _slowest_endpoints(samples),
    }
