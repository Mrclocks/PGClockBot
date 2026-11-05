"""Manual reconnect of the single PasarGuard link for the owner.

Resets the cached HTTP client and read circuits, then performs one short
authenticated GET /api/system so link-health stats and the circuit see a real
probe. Rate-limited process-wide; concurrent clicks share one asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.services import pg_link_health
from app.services.pasarguard import PasarGuardError, get_pg, reset_pg
from app.services.pg_read_resilience import PgReadUnavailable, web_read_budget
from app.services.security_policy import UnsafePgUrlError

logger = logging.getLogger(__name__)

PG_LINK_RECONNECT_PERMISSION = "pg_link.reconnect"
RECONNECT_RATE_LIMIT_SEC = 10.0
RECONNECT_BUDGET_SEC = 5.0
RECONNECT_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
RATE_LIMIT_MESSAGE = "چند ثانیه دیگر دوباره تلاش کنید"
SUCCESS_MESSAGE_TEMPLATE = "اتصال برقرار شد ({latency} ms)"

_lock = asyncio.Lock()
_last_reconnect_at = 0.0


@dataclass(slots=True)
class ReconnectResult:
    ok: bool
    latency_ms: int | None
    failure_kind: str | None
    rate_limited: bool = False


def is_reconnect_locked() -> bool:
    """True while a manual reconnect holds the process-wide lock."""
    return _lock.locked()


def reset_reconnect_gate() -> None:
    """Clear the rate-limit timestamp (tests only)."""
    global _last_reconnect_at
    _last_reconnect_at = 0.0


def flash_for_result(result: ReconnectResult) -> tuple[str, str]:
    """Return ``(ok|err, persian_message)`` for the home redirect flash."""
    if result.rate_limited:
        return "err", RATE_LIMIT_MESSAGE
    if result.ok:
        latency = int(result.latency_ms or 0)
        return "ok", SUCCESS_MESSAGE_TEMPLATE.format(latency=latency)
    label = pg_link_health.KIND_LABELS.get(result.failure_kind or "", None)
    return "err", label or pg_link_health.KIND_LABELS["dropped"]


def _failure_kind_from_exc(exc: BaseException) -> str:
    if isinstance(exc, PasarGuardError):
        return pg_link_health.failure_kind(status_code=exc.status_code)
    if isinstance(exc, PgReadUnavailable):
        cause = exc.__cause__
        if isinstance(cause, BaseException):
            return pg_link_health.failure_kind(cause)
        recorded = pg_link_health.latest_failure_kind()
        if recorded in pg_link_health.KIND_LABELS:
            return recorded
        return "gateway"
    if isinstance(exc, UnsafePgUrlError):
        return "dns"
    return pg_link_health.failure_kind(exc)


async def _probe_system() -> ReconnectResult:
    """Reset the client, then one budgeted authenticated GET /api/system."""
    reset_pg()
    client = get_pg()
    started = time.monotonic()
    try:
        with web_read_budget(seconds=RECONNECT_BUDGET_SEC):
            response = await client.get_system_probe(timeout=RECONNECT_TIMEOUT)
        if response.status_code >= 400:
            # 4xx: panel answered — reconnect result carries the kind; do not
            # pollute link-health failure counters (panel_read_request already
            # skips them). 5xx that did not raise still maps for the flash.
            kind = pg_link_health.failure_kind(status_code=response.status_code)
            return ReconnectResult(ok=False, latency_ms=None, failure_kind=kind)
    except (PgReadUnavailable, PasarGuardError, httpx.HTTPError, TimeoutError, UnsafePgUrlError) as exc:
        kind = _failure_kind_from_exc(exc)
        logger.warning("PasarGuard reconnect probe failed kind=%s", kind)
        return ReconnectResult(ok=False, latency_ms=None, failure_kind=kind)
    latency_ms = max(0, round((time.monotonic() - started) * 1000))
    return ReconnectResult(ok=True, latency_ms=latency_ms, failure_kind=None)


async def reconnect_panel() -> ReconnectResult:
    """Owner-triggered reconnect: one probe under the process-wide rate limit."""
    global _last_reconnect_at
    async with _lock:
        now = time.monotonic()
        if _last_reconnect_at > 0.0 and (now - _last_reconnect_at) < RECONNECT_RATE_LIMIT_SEC:
            return ReconnectResult(ok=False, latency_ms=None, failure_kind=None, rate_limited=True)
        _last_reconnect_at = now
        return await _probe_system()
