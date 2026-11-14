"""Immutable measurements and endpoint health without transport or database types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.py_compat import StrEnum

FRESHNESS = timedelta(seconds=180)
SAMPLE_LIMIT = 5
SLOW_DELAY_MS = 2000


class ConnectionStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"
    NOT_READY = "not_ready"
    PAUSED = "paused"


class ObservationKind(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"
    NOT_READY = "not_ready"


class MonitorReason(StrEnum):
    CORE_UNAVAILABLE = "core_unavailable"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    USER_MISSING = "user_missing"
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    CONTROL_UNAVAILABLE = "control_unavailable"
    PROXY_FAILED = "proxy_failed"
    FALLBACK_DESTINATION = "fallback_destination"
    PROBE_UNAVAILABLE = "probe_unavailable"
    SNAPSHOT_INVALID = "snapshot_invalid"
    PAUSED = "monitor_paused"
    STALE = "stale"
    SLOW = "slow_delay"
    RECOVERING = "recovering"
    NO_OBSERVATION = "no_observation"


def require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Monitoring timestamps must be timezone aware")


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    kind: ObservationKind
    checked_at: datetime
    destination: str | None = None
    delay_ms: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.checked_at)
        if self.kind == ObservationKind.SUCCESS:
            if type(self.delay_ms) is not int or not 0 <= self.delay_ms <= 5000 or not self.destination:
                raise ValueError("Invalid successful monitoring sample")
        elif self.delay_ms is not None:
            raise ValueError("Failed monitoring samples have no delay")


@dataclass(frozen=True, slots=True)
class EndpointIdentity:
    endpoint_key: str
    generation: str


@dataclass(frozen=True, slots=True)
class LatencySample:
    checked_at: datetime
    destination: str
    delay_ms: int


@dataclass(frozen=True, slots=True)
class EndpointHealth:
    identity: EndpointIdentity
    status: ConnectionStatus = ConnectionStatus.UNKNOWN
    reason: str | None = None
    checked_at: datetime | None = None
    destination: str | None = None
    delay_ms: int | None = None
    success_streak: int = 0
    failure_streak: int = 0
    has_observation: bool = False
    recovery_required: bool = False
    samples: tuple[LatencySample, ...] = ()


@dataclass(frozen=True, slots=True)
class HostHealth:
    status: ConnectionStatus
    checked_at: datetime | None
    delay_ms: int | None
    covered: int
    expected: int
