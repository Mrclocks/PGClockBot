"""Deterministic freshness, recovery, delay windows and complete-host coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from math import ceil
from statistics import median

from app.services.connection_status_types import (
    FRESHNESS,
    SAMPLE_LIMIT,
    SLOW_DELAY_MS,
    ConnectionStatus,
    EndpointHealth,
    EndpointIdentity,
    HostHealth,
    LatencySample,
    ObservationKind,
    ProbeObservation,
    require_aware,
)


def is_fresh(checked_at: datetime | None, *, now: datetime, freshness: timedelta = FRESHNESS) -> bool:
    require_aware(now)
    if checked_at is None:
        return False
    require_aware(checked_at)
    return checked_at <= now < checked_at + freshness


def apply_observation(identity: EndpointIdentity, observation: ProbeObservation, *,
                      previous: EndpointHealth | None = None, freshness: timedelta = FRESHNESS) -> EndpointHealth:
    state = previous if previous is not None and previous.identity == identity else EndpointHealth(identity)
    if not is_fresh(state.checked_at, now=observation.checked_at, freshness=freshness):
        state = replace(state, success_streak=0, failure_streak=0, samples=(), recovery_required=state.has_observation)
    if observation.kind in (ObservationKind.UNKNOWN, ObservationKind.NOT_READY):
        return replace(
            state, status=ConnectionStatus(observation.kind.value), reason=observation.reason,
            checked_at=observation.checked_at, delay_ms=None, destination=None, samples=(),
            success_streak=0, failure_streak=0, recovery_required=state.has_observation,
        )
    if observation.kind == ObservationKind.FAILURE:
        failures = min(state.failure_streak + 1, 3)
        return replace(
            state, status=ConnectionStatus.DOWN if failures == 3 else ConnectionStatus.DEGRADED,
            reason=observation.reason, checked_at=observation.checked_at, delay_ms=None,
            destination=observation.destination, samples=(), failure_streak=failures, success_streak=0,
            has_observation=True, recovery_required=True,
        )
    return _apply_success(state, observation, freshness=freshness)


def _apply_success(state: EndpointHealth, observation: ProbeObservation, *, freshness: timedelta) -> EndpointHealth:
    samples = tuple(sample for sample in state.samples
                    if sample.destination == observation.destination
                    and is_fresh(sample.checked_at, now=observation.checked_at, freshness=freshness))
    samples = (*samples, LatencySample(observation.checked_at, observation.destination, observation.delay_ms))[-SAMPLE_LIMIT:]
    delay_ms = ceil(median(sample.delay_ms for sample in samples))
    successes = min(state.success_streak + 1, 2)
    recovered = not state.recovery_required or successes >= 2
    status = ConnectionStatus.HEALTHY if recovered and delay_ms <= SLOW_DELAY_MS else ConnectionStatus.DEGRADED
    return replace(
        state, status=status, reason=observation.reason, checked_at=observation.checked_at,
        destination=observation.destination, delay_ms=delay_ms, samples=samples,
        success_streak=successes, failure_streak=0, has_observation=True,
        recovery_required=status != ConnectionStatus.HEALTHY,
    )


def aggregate_host(states: tuple[EndpointHealth, ...], *, expected: tuple[EndpointIdentity, ...],
                   now: datetime, enabled: bool = True, freshness: timedelta = FRESHNESS) -> HostHealth:
    require_aware(now)
    if not enabled:
        return HostHealth(ConnectionStatus.PAUSED, None, None, 0, len(expected))
    by_identity = {state.identity: state for state in states}
    valid = tuple(by_identity[identity] for identity in expected if identity in by_identity
                  and is_fresh(by_identity[identity].checked_at, now=now, freshness=freshness)
                  and by_identity[identity].status not in (ConnectionStatus.UNKNOWN, ConnectionStatus.NOT_READY, ConnectionStatus.PAUSED))
    if not expected or len(set(expected)) != len(expected) or len(valid) != len(expected) or len(by_identity) != len(states):
        return HostHealth(ConnectionStatus.UNKNOWN, None, None, len(valid), len(expected))
    statuses = {state.status for state in valid}
    if statuses == {ConnectionStatus.HEALTHY}:
        status = ConnectionStatus.HEALTHY
    elif statuses == {ConnectionStatus.DOWN}:
        status = ConnectionStatus.DOWN
    else:
        status = ConnectionStatus.DEGRADED
    delays = [state.delay_ms for state in valid if state.delay_ms is not None]
    return HostHealth(status, min(state.checked_at for state in valid), max(delays) if delays else None, len(valid), len(expected))
