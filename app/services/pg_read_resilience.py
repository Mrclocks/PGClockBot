"""Bound web-panel reads without cancelling handlers or PasarGuard mutations."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings
from app.py_compat import timeout as async_timeout
from app.services import pg_link_health
from app.services.security_policy import UnsafePgUrlError, assert_safe_pg_base_url

logger = logging.getLogger(__name__)

# First pause after an outage is confirmed; repeated failed recovery probes
# double it up to the ceiling so a dead panel is not hammered.
READ_COOLDOWN_SEC = 5.0
MAX_READ_COOLDOWN_SEC = 60.0
# One slow or dropped read must not hide PasarGuard from every shop: reads
# pause only after this many outage signals inside the window.
FAILURES_TO_PAUSE = 3
FAILURE_WINDOW_SEC = 30.0
# Gateway errors mean the panel behind a reverse proxy is down; other 5xx
# answers come from one endpoint of a reachable panel.
GATEWAY_OUTAGE_STATUSES = frozenset({502, 503, 504})
DNS_CACHE_TTL_SEC = 60.0
MAX_CONCURRENT_READS = 8
MAX_PANEL_STATES = 16
# Page budgets (default ~4s) fire before httpx's 30s timeout. A TimeoutError
# from async_timeout is a real panel outage only when THIS read was allowed to
# wait at least this long; shorter remainders mean earlier reads of the same
# page already consumed the shared budget.
MIN_OUTAGE_WAIT_SEC = 2.0
# A dropped keep-alive socket or a reset mid-read is usually gone on the next
# try. Timeouts are not retried: they would only double the wait.
TRANSIENT_READ_ERRORS = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)
READ_RETRY_DELAY_SEC = 0.2
READ_RETRY_MIN_REMAINING_SEC = 0.5
# A reverse proxy in front of PasarGuard often answers 502/503/504 for a
# moment while the panel restarts or a worker recycles. GET only, once,
# after a short random pause so many clients do not retry together.
GATEWAY_RETRY_STATUSES = frozenset({502, 503, 504})
GATEWAY_RETRY_DELAY_RANGE_SEC = (0.2, 0.6)
_jitter = secrets.SystemRandom()
PG_READ_UNAVAILABLE_MESSAGE = (
    "ارتباط با پاسارگارد کند یا قطع است؛ اطلاعات این بخش فعلاً در دسترس نیست. "
    "چند لحظه دیگر دوباره تلاش کنید."
)


class PgReadUnavailable(httpx.TimeoutException):
    """A read could not complete within the web panel's waiting budget."""


@dataclass(slots=True)
class ReadBudget:
    deadline: float
    failure: str | None = None
    # Once the panel looked unreachable, further reads of this page fail fast.
    origin_down: bool = False
    # Polling reads honour a pause but never start one.
    background: bool = False


@dataclass(slots=True)
class _PanelState:
    blocked_until: float = 0.0
    in_flight: int = 0
    probing: bool = False
    # Probe lease expiry (monotonic). A lost/slow probe must not block forever.
    probe_until: float = 0.0
    generation: int = 0
    pause_count: int = 0
    recent_failures: deque[float] = field(default_factory=lambda: deque(maxlen=FAILURES_TO_PAUSE))
    dns_task: asyncio.Task[str] | None = None
    dns_valid_until: float = 0.0


_BUDGET: ContextVar[ReadBudget | None] = ContextVar("pg_web_read_budget", default=None)
_STATES: dict[str, _PanelState] = {}


def configured_read_budget_sec() -> float:
    return float(get_settings().pg_web_read_budget_sec)


@contextmanager
def web_read_budget(*, seconds: float | None = None, background: bool = False) -> Iterator[ReadBudget]:
    """Share one deadline across authentication, fallback reads and widgets."""
    parent = _BUDGET.get()
    if parent is None:
        allowed = configured_read_budget_sec() if seconds is None else seconds
        budget = ReadBudget(time.monotonic() + allowed, background=background)
    else:
        budget = parent
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def reset_read_circuits() -> None:
    """Credential/base changes must permit a new connection attempt."""
    _STATES.clear()


def finish_web_read_budget() -> None:
    """Keep mutation work and its spawned jobs on their existing timeouts."""
    _BUDGET.set(None)


def _panel_state(base_url: str) -> _PanelState:
    # Only connection health is shared; response data and ACL stay client-scoped.
    origin = str(httpx.URL(base_url).copy_with(path="/", query=None, fragment=None))
    if origin not in _STATES:
        if len(_STATES) >= MAX_PANEL_STATES:
            idle = next((key for key, value in _STATES.items() if not value.in_flight), None)
            if idle is not None:
                _STATES.pop(idle)
            else:
                raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE)
        _STATES[origin] = _PanelState()
    return _STATES[origin]


def _reject(budget: ReadBudget, reason: str) -> None:
    budget.failure = reason
    raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE)


def _probe_lease_active(state: _PanelState, now: float) -> bool:
    return state.probe_until > now


def _clear_probe_lease(state: _PanelState) -> None:
    state.probing = False
    state.probe_until = 0.0


def _budget_timeout_is_outage(claimed_remaining: float) -> bool:
    """True when the claimed wait was long enough to count as a panel outage."""
    return claimed_remaining >= MIN_OUTAGE_WAIT_SEC


def _claim_read(state: _PanelState, budget: ReadBudget) -> tuple[float, int, bool]:
    """Reserve a read slot. Returns ``(remaining, generation, is_probe)``."""
    now = time.monotonic()
    remaining = budget.deadline - now
    if remaining <= 0:
        _reject(budget, "timeout")
    if budget.origin_down:
        _reject(budget, budget.failure or "unavailable")
    if state.blocked_until > now:
        _reject(budget, "unavailable")
    if _probe_lease_active(state, now):
        _reject(budget, "unavailable")
    if state.in_flight >= MAX_CONCURRENT_READS:
        _reject(budget, "busy")
    # After cooldown only one read may test recovery, preventing a retry storm.
    # Lease the probe so a hung probe cannot reject others forever.
    is_probe = state.blocked_until > 0
    if is_probe:
        state.probing = True
        state.probe_until = now + remaining
    state.in_flight += 1
    return remaining, state.generation, is_probe


def _record_outage(
    state: _PanelState,
    budget: ReadBudget,
    reason: str,
    *,
    is_probe: bool = False,
) -> None:
    """Fail the rest of this page fast; pause all reads once the outage is confirmed."""
    budget.failure = reason
    budget.origin_down = True
    # Any read that started before this failure must not reopen the circuit
    # while the pause is still active (see _maybe_mark_healthy).
    state.generation += 1
    if budget.background:
        return
    now = time.monotonic()
    if state.blocked_until > now:
        # Already paused; a straggler that started earlier must not extend it.
        return
    state.recent_failures.append(now)
    recent = [moment for moment in state.recent_failures if now - moment <= FAILURE_WINDOW_SEC]
    # Only a read that claimed the recovery probe may double the cooldown.
    # A straggler that started before the pause must not count as a failed probe
    # after the pause has expired.
    if is_probe or len(recent) >= FAILURES_TO_PAUSE:
        _pause_reads(state, reason, now)


def _pause_reads(state: _PanelState, reason: str, now: float) -> None:
    state.pause_count += 1
    cooldown = min(READ_COOLDOWN_SEC * 2 ** (state.pause_count - 1), MAX_READ_COOLDOWN_SEC)
    state.blocked_until = now + cooldown
    state.recent_failures.clear()
    logger.warning(
        "PasarGuard web reads paused reason=%s retry_after=%ss recent=%s",
        reason, cooldown, pg_link_health.summary_text(),
    )
    try:
        from app.services.pg_link_alerts import schedule_circuit_pause_alert

        schedule_circuit_pause_alert(reason if reason in pg_link_health.KIND_LABELS else None)
    except Exception:
        logger.debug("PasarGuard link alert schedule after pause failed", exc_info=True)


def _mark_healthy(state: _PanelState) -> None:
    state.blocked_until = 0.0
    state.pause_count = 0
    state.recent_failures.clear()
    _clear_probe_lease(state)


def _maybe_mark_healthy(state: _PanelState, *, started_generation: int) -> None:
    """Heal after a success once the pause has expired.

    Generation drift from unrelated failures does not block healing after the
    pause window. A success from a read that started before the latest confirmed
    outage is ignored only while the circuit is still paused.
    """
    now = time.monotonic()
    still_paused = state.blocked_until > now
    started_before_latest_outage = state.generation != started_generation
    if still_paused and started_before_latest_outage:
        return
    _mark_healthy(state)


def note_successful_panel_contact(base_url: str) -> None:
    """Budget-less reads and mutations: heal an expired pause without starting one."""
    try:
        state = _panel_state(base_url)
    except PgReadUnavailable:
        return
    now = time.monotonic()
    if state.blocked_until > now:
        return
    if state.blocked_until > 0.0 or state.pause_count > 0 or state.recent_failures:
        _mark_healthy(state)


def force_mark_circuit_healthy(base_url: str) -> None:
    """Watchdog/manual recovery: clear a pause after a verified successful probe."""
    try:
        state = _panel_state(base_url)
    except PgReadUnavailable:
        return
    _mark_healthy(state)


def _consume_dns_result(task: asyncio.Task[str], state: _PanelState) -> None:
    # A request may time out while libc is still resolving in a worker thread.
    # Retrieve late failures; future requests can reuse this same lookup.
    if not task.cancelled():
        if task.exception() is None:
            state.dns_valid_until = time.monotonic() + DNS_CACHE_TTL_SEC


async def validate_panel_base(base_url: str, *, protected: bool = True) -> str:
    """Keep hostname validation off the event loop, before sending credentials."""
    budget = _BUDGET.get() if protected else None
    if budget is None:
        return await asyncio.to_thread(assert_safe_pg_base_url, base_url)
    state = _panel_state(base_url)
    remaining, _generation, is_probe = _claim_read(state, budget)
    try:
        if state.dns_task is not None and state.dns_task.done() and state.dns_valid_until <= time.monotonic():
            state.dns_task = None
        if state.dns_task is None:
            state.dns_task = asyncio.create_task(asyncio.to_thread(assert_safe_pg_base_url, base_url))
            state.dns_task.add_done_callback(lambda task: _consume_dns_result(task, state))
        async with async_timeout(remaining):
            await asyncio.shield(state.dns_task)
        # DNS is shared by origin, but each caller keeps its own API base path.
        return assert_safe_pg_base_url(base_url, resolve_dns=False)
    except TimeoutError as exc:
        if state.dns_task is not None and state.dns_task.done():
            state.dns_task = None
        if _budget_timeout_is_outage(remaining):
            pg_link_health.record_failure("timeout")
            _record_outage(state, budget, "timeout", is_probe=is_probe)
        else:
            # Earlier reads of this page left too little wait for a fair test.
            pg_link_health.record_budget_exhaustion()
            budget.failure = "timeout"
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE) from exc
    except UnsafePgUrlError as exc:
        if state.dns_task is not None and state.dns_task.done():
            state.dns_task = None
        pg_link_health.record_failure("dns")
        _record_outage(state, budget, "unavailable", is_probe=is_probe)
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE) from exc
    finally:
        state.in_flight -= 1
        if is_probe:
            _clear_probe_lease(state)


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    deadline: float | None,
    kwargs: dict[str, Any],
) -> httpx.Response:
    """Repeat a read once after a dropped connection or a gateway error, if the deadline leaves room."""
    try:
        response = await client.request(method, url, **kwargs)
    except TRANSIENT_READ_ERRORS:
        if not _has_room(deadline, READ_RETRY_DELAY_SEC):
            raise
        logger.debug("PasarGuard read interrupted; retrying once", exc_info=True)
        await asyncio.sleep(READ_RETRY_DELAY_SEC)
        return await client.request(method, url, **kwargs)
    if response.status_code not in GATEWAY_RETRY_STATUSES or method.upper() != "GET":
        return response
    delay = _jitter.uniform(*GATEWAY_RETRY_DELAY_RANGE_SEC)
    if not _has_room(deadline, delay):
        return response
    logger.debug("PasarGuard gateway answered %s; retrying once", response.status_code)
    await asyncio.sleep(delay)
    return await client.request(method, url, **kwargs)


def _has_room(deadline: float | None, delay: float) -> bool:
    return deadline is None or deadline - time.monotonic() >= delay + READ_RETRY_MIN_REMAINING_SEC


async def panel_read_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Bound only reads and token acquisition in a web request context.

    Jobs/bot calls retain their existing timeout. Mutation requests bypass this
    helper; neither their duration nor unknown-outcome handling is changed.
    """
    budget = _BUDGET.get()
    if budget is None:
        return await _budgetless_read(client, method, url, kwargs)
    state = _panel_state(str(client.base_url))
    remaining, generation, is_probe = _claim_read(state, budget)
    started = time.monotonic()
    try:
        async with async_timeout(remaining):
            response = await _request_with_retry(client, method, url, budget.deadline, kwargs)
    except httpx.TimeoutException as exc:
        pg_link_health.record_failure(pg_link_health.failure_kind(exc))
        _record_outage(state, budget, "timeout", is_probe=is_probe)
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE) from exc
    except TimeoutError as exc:
        # Classify by how long THIS read was claimed to wait, not by exception type.
        # Default page budget (~4s) fires before httpx's 30s timeout against a
        # panel that accepts the connection but never answers.
        if _budget_timeout_is_outage(remaining):
            pg_link_health.record_failure("timeout")
            _record_outage(state, budget, "timeout", is_probe=is_probe)
        else:
            # Unfair wait (earlier reads consumed the page budget): exhaustion only.
            # An unfair recovery probe must not double the cooldown or heal.
            pg_link_health.record_budget_exhaustion()
            budget.failure = "timeout"
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE) from exc
    except httpx.TransportError as exc:
        pg_link_health.record_failure(pg_link_health.failure_kind(exc))
        _record_outage(state, budget, "unavailable", is_probe=is_probe)
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE) from exc
    finally:
        state.in_flight -= 1
        # Only the read that claimed the probe lease may clear it.
        if is_probe:
            _clear_probe_lease(state)
    if response.status_code >= 500:
        pg_link_health.record_failure(pg_link_health.failure_kind(status_code=response.status_code))
    elif response.status_code < 400:
        pg_link_health.record_success(time.monotonic() - started)
    # 4xx: the panel answered; reconnect/caller maps the status — do not pollute stats.
    if response.status_code in GATEWAY_OUTAGE_STATUSES:
        _record_outage(state, budget, "unavailable", is_probe=is_probe)
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE)
    if response.status_code >= 500:
        # The panel answered: only this read failed, the circuit stays as it is.
        budget.failure = "unavailable"
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE)
    _maybe_mark_healthy(state, started_generation=generation)
    return response


async def _budgetless_read(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    kwargs: dict[str, Any],
) -> httpx.Response:
    """Bot/job reads: never start a pause; heal an expired pause on success."""
    response = await _request_with_retry(client, method, url, None, kwargs)
    if response.status_code < 500:
        note_successful_panel_contact(str(client.base_url))
    return response


def circuit_status() -> dict[str, object]:
    """Current pause state of the panel's read circuit, for the connection card."""
    now = time.monotonic()
    paused = [state for state in _STATES.values() if state.blocked_until > now]
    pause_counts = [state.pause_count for state in _STATES.values()]
    return {
        "paused": bool(paused),
        "retry_in_sec": max((round(state.blocked_until - now) for state in paused), default=0),
        "pause_count": max(pause_counts, default=0),
        "probing": any(_probe_lease_active(state, now) for state in _STATES.values()),
    }
