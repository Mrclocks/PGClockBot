"""Background probe of the single PasarGuard link.

One cheap authenticated GET /api/system per tick. Heals a paused circuit on
success (bypasses the read-circuit gate so recovery is possible). After K
consecutive failures, rebuilds the HTTP client once and backs off. Never
starts a pause. Skips while a manual reconnect holds its lock.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import pg_link_health
from app.services.pasarguard import PasarGuardError, get_pg
from app.services.pg_http_rebuild import HttpRebuildState, rebuild_holder_http_client
from app.services.pg_link_reconnect import is_reconnect_locked
from app.services.pg_read_resilience import force_mark_circuit_healthy
from app.services.users import get_setting

logger = logging.getLogger(__name__)

SETTING_KEY = "pg_link_watchdog_enabled"
SETTING_DEFAULT = "1"
FAILURES_BEFORE_REBUILD = 3
WATCHDOG_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
BACKOFF_TICKS_AFTER_REBUILD = 3

_consecutive_failures = 0
_backoff_ticks = 0
_rebuild_state = HttpRebuildState()


@dataclass(slots=True)
class WatchdogTickResult:
    ran: bool
    ok: bool
    skipped_reason: str | None
    rebuilt: bool
    failure_kind: str | None


def reset_watchdog_state() -> None:
    """Tests only."""
    global _consecutive_failures, _backoff_ticks, _rebuild_state
    _consecutive_failures = 0
    _backoff_ticks = 0
    _rebuild_state = HttpRebuildState()


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


async def watchdog_enabled(session: AsyncSession) -> bool:
    return _truthy(await get_setting(session, SETTING_KEY, SETTING_DEFAULT))


def _failure_kind_from_exc(exc: BaseException) -> str:
    if isinstance(exc, PasarGuardError):
        return pg_link_health.failure_kind(status_code=exc.status_code)
    return pg_link_health.failure_kind(exc)


async def _probe() -> tuple[bool, str | None]:
    """Authenticated GET via ``request()`` (401 re-login + rebuild hooks).

    No web_read_budget, so ``panel_read_request`` takes the budgetless path and
    does not reject while the circuit is paused — then we force-heal on success.
    """
    client = get_pg()
    started = time.monotonic()
    try:
        await client.request(
            "GET",
            "/api/system",
            use_cache=False,
            timeout=WATCHDOG_TIMEOUT,
        )
    except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
        kind = _failure_kind_from_exc(exc)
        pg_link_health.record_failure(kind)
        return False, kind
    pg_link_health.record_success(time.monotonic() - started)
    force_mark_circuit_healthy(str(client.base_url))
    return True, None


async def run_watchdog_tick(session: AsyncSession) -> WatchdogTickResult:
    """One watchdog cycle. Never raises."""
    global _consecutive_failures, _backoff_ticks
    try:
        if not await watchdog_enabled(session):
            return WatchdogTickResult(False, False, "disabled", False, None)
        if is_reconnect_locked():
            return WatchdogTickResult(False, False, "reconnect_lock", False, None)
        if _backoff_ticks > 0:
            _backoff_ticks -= 1
            return WatchdogTickResult(False, False, "backoff", False, None)

        try:
            ok, kind = await _probe()
        except Exception as exc:
            kind = _failure_kind_from_exc(exc)
            pg_link_health.record_failure(kind)
            logger.warning("PasarGuard link watchdog probe failed kind=%s", kind, exc_info=True)
            ok = False

        if ok:
            _consecutive_failures = 0
            return WatchdogTickResult(True, True, None, False, None)

        _consecutive_failures += 1
        rebuilt = False
        if _consecutive_failures >= FAILURES_BEFORE_REBUILD:
            client = get_pg()
            rebuilt = rebuild_holder_http_client(client, _rebuild_state, kind=kind or "dropped")
            if rebuilt:
                _consecutive_failures = 0
                _backoff_ticks = BACKOFF_TICKS_AFTER_REBUILD
        return WatchdogTickResult(True, False, None, rebuilt, kind)
    except Exception:
        logger.warning("PasarGuard link watchdog tick failed", exc_info=True)
        return WatchdogTickResult(False, False, "error", False, None)
