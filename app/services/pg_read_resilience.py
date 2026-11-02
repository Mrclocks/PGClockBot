"""Bound web-panel reads without cancelling handlers or PasarGuard mutations."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from app.py_compat import timeout as async_timeout
from app.services.security_policy import UnsafePgUrlError, assert_safe_pg_base_url

logger = logging.getLogger(__name__)

WEB_READ_BUDGET_SEC = 2.0
READ_COOLDOWN_SEC = 10.0
DNS_CACHE_TTL_SEC = 60.0
MAX_CONCURRENT_READS = 8
MAX_PANEL_STATES = 16
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


@dataclass(slots=True)
class _PanelState:
    blocked_until: float = 0.0
    in_flight: int = 0
    probing: bool = False
    generation: int = 0
    dns_task: asyncio.Task[str] | None = None
    dns_valid_until: float = 0.0


_BUDGET: ContextVar[ReadBudget | None] = ContextVar("pg_web_read_budget", default=None)
_STATES: dict[str, _PanelState] = {}


@contextmanager
def web_read_budget(*, seconds: float = WEB_READ_BUDGET_SEC) -> Iterator[ReadBudget]:
    """Share one deadline across authentication, fallback reads and widgets."""
    parent = _BUDGET.get()
    budget = parent or ReadBudget(time.monotonic() + seconds)
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


def _claim_read(state: _PanelState, budget: ReadBudget) -> tuple[float, int]:
    now = time.monotonic()
    remaining = budget.deadline - now
    if remaining <= 0:
        _reject(budget, "timeout")
    if state.blocked_until > now or state.probing:
        _reject(budget, "unavailable")
    if state.in_flight >= MAX_CONCURRENT_READS:
        _reject(budget, "busy")
    # After cooldown only one read may test recovery, preventing a retry storm.
    state.probing = state.blocked_until > 0
    state.in_flight += 1
    return remaining, state.generation


def _pause_reads(state: _PanelState, budget: ReadBudget, reason: str) -> None:
    if state.blocked_until <= time.monotonic():
        logger.warning("PasarGuard web reads paused reason=%s retry_after=%ss", reason, READ_COOLDOWN_SEC)
    state.generation += 1
    state.blocked_until = time.monotonic() + READ_COOLDOWN_SEC
    budget.failure = reason


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
    remaining, _generation = _claim_read(state, budget)
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
    except (TimeoutError, UnsafePgUrlError) as exc:
        if state.dns_task is not None and state.dns_task.done():
            state.dns_task = None
        _pause_reads(state, budget, "unavailable")
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE) from exc
    finally:
        state.in_flight -= 1
        state.probing = False


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
        return await _request_with_retry(client, method, url, None, kwargs)
    state = _panel_state(str(client.base_url))
    remaining, generation = _claim_read(state, budget)
    try:
        async with async_timeout(remaining):
            response = await _request_with_retry(client, method, url, budget.deadline, kwargs)
    except (TimeoutError, httpx.TransportError) as exc:
        _pause_reads(state, budget, "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "unavailable")
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE) from exc
    finally:
        state.in_flight -= 1
        state.probing = False
    if response.status_code >= 500:
        _pause_reads(state, budget, "unavailable")
        raise PgReadUnavailable(PG_READ_UNAVAILABLE_MESSAGE)
    if state.generation == generation:
        state.blocked_until = 0.0
    return response
