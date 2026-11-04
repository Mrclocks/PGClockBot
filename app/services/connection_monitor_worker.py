"""Bounded local monitoring cycles with committed claims and independent sessions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.py_compat import gather_or_cancel, timeout as async_timeout
from app.services.connection_monitor_accounts import MonitorAccountError
from app.services.connection_monitor_subscription import plan_monitor_configs
from app.services.connection_monitor_config import MonitorBinding
from app.services.connection_monitor_claims import (
    MonitorCycleClaim,
    MonitorTargetClaim,
    claim_due_targets,
    claim_monitor_cycle,
    release_cycle_targets,
    release_monitor_cycle,
)
from app.services.connection_monitor_panel import mark_monitor_user_missing, read_monitor_catalog
from app.services.connection_monitor_snapshots import complete_monitor_target, invalidate_monitor_evidence
from app.services.connection_monitor_sync import load_cycle_inputs, reconcile_monitor_plans
from app.services.connection_monitor_types import MonitorConfigError, MonitorConfigPlan
from app.services.connection_probe import probe_client, probe_control, probe_proxy
from app.services.connection_status_types import ConnectionStatus, MonitorReason, ObservationKind, ProbeObservation
from app.services.pasarguard import PasarGuardError
from app.services.xray_probe import XrayBatch, run_xray_batch
from app.services.xray_probe_process import finish_cleanup
from app.services.xray_probe_resources import XrayBinary, XrayProbeError


logger = logging.getLogger(__name__)
WORK_SECONDS = 45
PROXY_CONCURRENCY = 4
LEASE_CLEANUP_SECONDS = 1
PANEL_ACCOUNT_CODES = frozenset({"panel_timeout", "panel_unavailable", "panel_denied"})
# Only a confirmed deleted monitor user stops platform polling. A transient
# panel 404 mapped to ``not_found`` (wrong username at setup, blip, etc.) must
# not disable the whole monitor.
MISSING_USER_CODES = frozenset({"user_missing"})


@dataclass(frozen=True, slots=True)
class MonitorCycleResult:
    claimed: bool
    completed: int = 0
    reason: str | None = None


async def _finish_cycle(sessions: async_sessionmaker[AsyncSession], cycle: MonitorCycleClaim) -> None:
    try:
        async with async_timeout(LEASE_CLEANUP_SECONDS):
            async with sessions() as session:
                await release_cycle_targets(session, cycle)
                await release_monitor_cycle(session, cycle)
    except (TimeoutError, SQLAlchemyError):
        logger.warning("Monitoring lease cleanup deferred to expiry")


async def _record_issue(sessions: async_sessionmaker[AsyncSession], cycle: MonitorCycleClaim, *,
                         status: ConnectionStatus, reason: str) -> None:
    async with sessions() as session:
        await invalidate_monitor_evidence(session, cycle, status=status, reason=reason, now=datetime.now(timezone.utc))


async def _check_target(sessions: async_sessionmaker[AsyncSession], claim: MonitorTargetClaim,
                        batch: XrayBatch, semaphore: asyncio.Semaphore, *, control_ok: bool) -> bool:
    async def check(binding: MonitorBinding) -> tuple[str, ProbeObservation]:
        async with semaphore:
            client = await asyncio.to_thread(probe_client, port=binding.port)
            async with client:
                observation = await probe_proxy(client, control_ok=control_ok, account_valid=True, runner_alive=lambda: batch.is_alive)
                return binding.endpoint.endpoint_key, observation

    bindings = tuple(binding for binding in batch.bindings if binding.endpoint.config_key == claim.config_key)
    try:
        observations = dict(await gather_or_cancel(check(binding) for binding in bindings))
        async with sessions() as session:
            return await complete_monitor_target(session, claim, observations, now=datetime.now(timezone.utc))
    except Exception as exc:
        # Each target is an independent read-only network unit. Its lease remains
        # releasable and other targets must still complete if this unit fails.
        logger.warning("Monitoring target failed target=%s error_type=%s", claim.target_id, type(exc).__name__)
        return await _record_target_failure(sessions, claim, bindings)


async def _record_target_failure(sessions: async_sessionmaker[AsyncSession], claim: MonitorTargetClaim,
                                 bindings: tuple[MonitorBinding, ...]) -> bool:
    now = datetime.now(timezone.utc)
    observations = {binding.endpoint.endpoint_key: ProbeObservation(ObservationKind.UNKNOWN, now, reason="probe_unavailable")
                    for binding in bindings}
    try:
        async with sessions() as session:
            return await complete_monitor_target(session, claim, observations, now=now)
    except (SQLAlchemyError, ValueError):
        logger.warning("Monitoring target evidence unavailable target=%s", claim.target_id)
        return False


async def _probe_claims(sessions: async_sessionmaker[AsyncSession], claims: tuple[MonitorTargetClaim, ...],
                         plans: tuple[MonitorConfigPlan, ...], binary: XrayBinary) -> int:
    config_keys = {claim.config_key for claim in claims}
    endpoints = tuple(endpoint for plan in plans if plan.config_key in config_keys for endpoint in plan.endpoints)
    semaphore = asyncio.Semaphore(PROXY_CONCURRENCY)
    control = asyncio.create_task(probe_control())
    try:
        async with run_xray_batch(binary, endpoints) as batch:
            control_ok = await control
            results = await gather_or_cancel(
                _check_target(sessions, claim, batch, semaphore, control_ok=control_ok) for claim in claims
            )
            return sum(results)
    finally:
        control.cancel()
        await asyncio.gather(control, return_exceptions=True)


async def _run_work(sessions: async_sessionmaker[AsyncSession], cycle: MonitorCycleClaim, binary: XrayBinary) -> int:
    async with sessions() as session:
        account, selected = await load_cycle_inputs(session, cycle, now=datetime.now(timezone.utc))
    if not selected:
        return 0
    try:
        catalog = await read_monitor_catalog(account)
    except TimeoutError:
        # Align mocked/direct timeouts with panel_account_error codes.
        raise MonitorAccountError("panel_timeout") from None
    plans = await asyncio.to_thread(
        plan_monitor_configs, native_configs=catalog.native_configs,
        selected_keys=selected, account_revision=account.revision,
    )
    async with sessions() as session:
        if not await reconcile_monitor_plans(session, cycle, plans, now=datetime.now(timezone.utc)):
            return 0
        claims = await claim_due_targets(session, cycle, now=datetime.now(timezone.utc))
    if not claims:
        return 0
    return await _probe_claims(sessions, claims, plans, binary)


async def run_monitor_cycle(sessions: async_sessionmaker[AsyncSession], binary: XrayBinary) -> MonitorCycleResult:
    async with sessions() as session:
        cycle = await claim_monitor_cycle(session, now=datetime.now(timezone.utc))
    if cycle is None:
        return MonitorCycleResult(False)
    try:
        async with async_timeout(WORK_SECONDS):
            completed = await _run_work(sessions, cycle, binary)
        return MonitorCycleResult(True, completed)
    except XrayProbeError:
        await _record_issue(sessions, cycle, status=ConnectionStatus.NOT_READY, reason="core_unavailable")
        return MonitorCycleResult(True, reason="core_unavailable")
    except MonitorAccountError as exc:
        if exc.code in MISSING_USER_CODES:
            try:
                async with sessions() as session:
                    await mark_monitor_user_missing(
                        session, state_id=cycle.state_id, now=datetime.now(timezone.utc),
                    )
            except Exception:
                logger.warning(
                    "Monitoring could not disable missing panel user state=%s",
                    cycle.state_id,
                    exc_info=True,
                )
            reason = MonitorReason.USER_MISSING.value
            await _record_issue(sessions, cycle, status=ConnectionStatus.PAUSED, reason=reason)
            return MonitorCycleResult(True, reason=reason)
        if exc.code in PANEL_ACCOUNT_CODES:
            status, reason = ConnectionStatus.UNKNOWN, "catalog_unavailable"
        else:
            status, reason = ConnectionStatus.NOT_READY, "account_unavailable"
        await _record_issue(sessions, cycle, status=status, reason=reason)
        return MonitorCycleResult(True, reason=reason)
    except MonitorConfigError as exc:
        reason = exc.issue.value
        await _record_issue(sessions, cycle, status=ConnectionStatus.NOT_READY, reason=reason)
        return MonitorCycleResult(True, reason=reason)
    except (PasarGuardError, httpx.HTTPError):
        await _record_issue(sessions, cycle, status=ConnectionStatus.UNKNOWN, reason="catalog_unavailable")
        return MonitorCycleResult(True, reason="catalog_unavailable")
    except ValueError as exc:
        logger.warning("Monitoring cycle failed error_type=%s", type(exc).__name__)
        await _record_issue(sessions, cycle, status=ConnectionStatus.UNKNOWN, reason="probe_unavailable")
        return MonitorCycleResult(True, reason="probe_unavailable")
    except TimeoutError:
        # Missing a turn never becomes a synthetic route failure.
        return MonitorCycleResult(True, reason="cycle_deadline")
    finally:
        await finish_cleanup(_finish_cycle(sessions, cycle))
