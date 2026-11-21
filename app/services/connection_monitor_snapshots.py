"""Bounded snapshot conversion and token-fenced atomic measurement completion."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorTarget
from app.services.connection_monitor_claims import MonitorCycleClaim, MonitorTargetClaim, current_monitor_cycle
from app.services.connection_monitor_settings import LOCAL_PROBE_ID
from app.services.connection_monitor_timing import DEFAULT_TIMING, MonitorTiming
from app.services.connection_monitor_types import MAX_MONITOR_ENDPOINTS
from app.services.connection_status_policy import apply_observation, is_fresh
from app.services.connection_status_types import (
    SAMPLE_LIMIT,
    ConnectionStatus,
    EndpointHealth,
    EndpointIdentity,
    LatencySample,
    ProbeObservation,
)


logger = logging.getLogger(__name__)


def stored_utc(value: datetime | None) -> datetime | None:
    # Stored timestamps are UTC; a naive value (from Python code or a test) is read as UTC.
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _samples(raw: str) -> tuple[LatencySample, ...]:
    if len(raw) > 8192:
        raise ValueError("Invalid monitoring sample window")
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) > SAMPLE_LIMIT:
        raise ValueError("Invalid monitoring sample window")
    samples = []
    for value in values:
        if not isinstance(value, dict) or type(value.get("delay_ms")) is not int or not 0 <= value["delay_ms"] <= 5000:
            raise ValueError("Invalid monitoring sample")
        checked_at = datetime.fromisoformat(value["checked_at"])
        if checked_at.tzinfo is None or value["destination"] not in ("gstatic", "cloudflare"):
            raise ValueError("Invalid monitoring sample")
        samples.append(LatencySample(checked_at, value["destination"], value["delay_ms"]))
    return tuple(samples)


def snapshot_health(row: ConnectionMonitorSnapshot) -> EndpointHealth:
    identity = EndpointIdentity(row.endpoint_key, row.generation)
    try:
        return EndpointHealth(
            identity, ConnectionStatus(row.status), row.reason, stored_utc(row.checked_at), row.destination,
            row.delay_ms, row.success_streak, row.failure_streak, row.has_observation, row.recovery_required,
            _samples(row.samples_json),
        )
    except (ValueError, KeyError, TypeError):
        logger.warning("Invalid monitoring snapshot target=%s", row.target_id)
        return EndpointHealth(identity, reason="snapshot_invalid", has_observation=True, recovery_required=True)


def _write_health(row: ConnectionMonitorSnapshot, state: EndpointHealth) -> None:
    for name in ("status", "reason", "checked_at", "destination", "delay_ms", "success_streak",
                 "failure_streak", "has_observation", "recovery_required"):
        setattr(row, name, getattr(state, name))
    row.samples_json = json.dumps([{
        "checked_at": sample.checked_at.isoformat(), "destination": sample.destination, "delay_ms": sample.delay_ms,
    } for sample in state.samples], separators=(",", ":"))


async def complete_monitor_target(session: AsyncSession, claim: MonitorTargetClaim,
                                  observations: dict[str, ProbeObservation], *, now: datetime,
                                  timing: MonitorTiming = DEFAULT_TIMING) -> bool:
    if not observations or len(observations) > MAX_MONITOR_ENDPOINTS:
        raise ValueError("Invalid monitoring endpoint coverage")
    if any(not is_fresh(observation.checked_at, now=now, freshness=timing.freshness) for observation in observations.values()):
        return False
    rows = (await session.scalars(select(ConnectionMonitorSnapshot).where(
        ConnectionMonitorSnapshot.reseller_id.is_(None), ConnectionMonitorSnapshot.target_id == claim.target_id,
        ConnectionMonitorSnapshot.probe_id == LOCAL_PROBE_ID, ConnectionMonitorSnapshot.generation == claim.generation,
    ).limit(MAX_MONITOR_ENDPOINTS + 1))).all()
    if {row.endpoint_key for row in rows} != observations.keys():
        await session.rollback()
        return False
    result = await session.execute(update(ConnectionMonitorTarget).where(
        ConnectionMonitorTarget.id == claim.target_id, ConnectionMonitorTarget.reseller_id.is_(None),
        ConnectionMonitorTarget.test_enabled.is_(True), ConnectionMonitorTarget.generation == claim.generation,
        ConnectionMonitorTarget.endpoint_count == len(observations), ConnectionMonitorTarget.claim_token == claim.token,
        ConnectionMonitorTarget.claim_until > now, current_monitor_cycle(claim.cycle, now=now),
    ).values(claim_token=None, claim_until=None, manual_pending=False, next_check_at=now + timing.interval))
    if result.rowcount != 1:
        await session.rollback()
        return False
    for row in rows:
        state = apply_observation(
            EndpointIdentity(row.endpoint_key, claim.generation), observations[row.endpoint_key],
            previous=snapshot_health(row), freshness=timing.freshness,
        )
        _write_health(row, state)
    await session.commit()
    return True


async def invalidate_monitor_evidence(session: AsyncSession, cycle: MonitorCycleClaim, *,
                                      status: ConnectionStatus, reason: str, now: datetime) -> None:
    targets = select(ConnectionMonitorTarget.id).where(
        ConnectionMonitorTarget.reseller_id.is_(None), ConnectionMonitorTarget.test_enabled.is_(True),
    ).limit(MAX_MONITOR_ENDPOINTS)
    await session.execute(update(ConnectionMonitorSnapshot).where(
        ConnectionMonitorSnapshot.reseller_id.is_(None), ConnectionMonitorSnapshot.target_id.in_(targets),
        ConnectionMonitorSnapshot.probe_id == LOCAL_PROBE_ID, current_monitor_cycle(cycle, now=now),
    ).values(status=status.value, reason=reason, checked_at=now, delay_ms=None, success_streak=0,
             failure_streak=0, recovery_required=ConnectionMonitorSnapshot.has_observation, samples_json="[]"))
    await session.commit()
