"""Rebuild a PasarGuard httpx client after repeated transport failures.

In-flight requests on the retired client are left alone (graceful close).
5xx/4xx answers and page-budget timeouts do not count. Rebuilds are
rate-limited so a flapping link cannot thrash the pool.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.services.pg_read_resilience import PgReadUnavailable

logger = logging.getLogger(__name__)

TRANSPORT_FAILURES_TO_REBUILD = 5
TRANSPORT_FAILURE_WINDOW_SEC = 60.0
CLIENT_REBUILD_MIN_INTERVAL_SEC = 30.0


def counts_as_transport_outage(exc: BaseException) -> bool:
    """True for genuine transport problems, not budget exhaustion or HTTP answers."""
    if isinstance(exc, PgReadUnavailable):
        cause = exc.__cause__
        return cause is not None and counts_as_transport_outage(cause)
    # Bare TimeoutError is the shared page budget; httpx timeouts are panel-side.
    if type(exc) is TimeoutError:
        return False
    return isinstance(exc, httpx.TransportError)


@dataclass(slots=True)
class HttpRebuildState:
    failures: deque[float] = field(
        default_factory=lambda: deque(maxlen=TRANSPORT_FAILURES_TO_REBUILD)
    )
    last_rebuild_at: float = 0.0
    rebuild_count: int = 0


def note_http_success(state: HttpRebuildState) -> None:
    state.failures.clear()


def rebuild_holder_http_client(holder: Any, state: HttpRebuildState, *, kind: str) -> bool:
    """Swap ``holder._client`` for a fresh pool and retire the old one with grace."""
    # Late import: pasarguard imports this module for the request path.
    from app.services.pasarguard import _retire_http_client, new_panel_http_client

    base_url = str(getattr(holder, "base_url", "") or "")
    old = getattr(holder, "_client", None)
    if old is None:
        return False
    holder._client = new_panel_http_client(base_url)
    state.last_rebuild_at = time.monotonic()
    state.rebuild_count += 1
    state.failures.clear()
    if isinstance(old, httpx.AsyncClient):
        _retire_http_client(old)
    logger.warning("PasarGuard HTTP client rebuilt kind=%s", kind)
    return True


def note_http_transport_failure(
    holder: Any,
    state: HttpRebuildState,
    exc: BaseException,
    *,
    kind: str,
) -> bool:
    """Record a transport failure; maybe rebuild ``holder._client``. Returns True if rebuilt."""
    if not counts_as_transport_outage(exc):
        return False
    now = time.monotonic()
    state.failures.append(now)
    recent = [moment for moment in state.failures if now - moment <= TRANSPORT_FAILURE_WINDOW_SEC]
    if len(recent) < TRANSPORT_FAILURES_TO_REBUILD:
        return False
    if state.last_rebuild_at > 0.0 and (now - state.last_rebuild_at) < CLIENT_REBUILD_MIN_INTERVAL_SEC:
        return False
    return rebuild_holder_http_client(holder, state, kind=kind)
