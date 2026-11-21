"""Reconcile fresh route plans in one short, scoped cycle transaction."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget
from app.services.connection_monitor_claims import MonitorCycleClaim, current_monitor_cycle
from app.services.connection_monitor_panel import MonitorAccount
from app.services.connection_monitor_settings import ACCOUNT_UNAVAILABLE, LOCAL_PROBE_ID
from app.services.connection_monitor_snapshots import stored_utc
from app.services.connection_monitor_types import MAX_MONITOR_ENDPOINTS, MonitorConfigError, MonitorConfigPlan, MonitorIssue


async def load_cycle_inputs(session: AsyncSession, cycle: MonitorCycleClaim, *, now: datetime) -> tuple[MonitorAccount, tuple[str, ...]]:
    state = await session.scalar(select(ConnectionMonitorState).where(
        ConnectionMonitorState.id == cycle.state_id, ConnectionMonitorState.reseller_id.is_(None),
        current_monitor_cycle(cycle, now=now),
    ).limit(1))
    if state is None:
        raise ValueError(ACCOUNT_UNAVAILABLE)
    account = MonitorAccount(state.pg_user_id, state.account_revision, state.subscription_token_enc, state.pg_username)
    config_keys = tuple((await session.scalars(select(ConnectionMonitorTarget.subscription_key).where(
        ConnectionMonitorTarget.reseller_id.is_(None), ConnectionMonitorTarget.test_enabled.is_(True),
        ConnectionMonitorTarget.subscription_key.is_not(None),
    ).order_by(ConnectionMonitorTarget.id).limit(MAX_MONITOR_ENDPOINTS + 1))).all())
    await session.commit()
    return account, config_keys


async def cycle_has_work(session: AsyncSession, cycle: MonitorCycleClaim, *, now: datetime, interval: timedelta) -> bool:
    """True when a checkable target is due (scheduled or manual) or the catalog is one interval old.

    Otherwise the round would only re-read the panel catalog and start the xray core for nothing.
    """
    synced_at = await session.scalar(select(ConnectionMonitorState.catalog_synced_at).where(
        ConnectionMonitorState.id == cycle.state_id, ConnectionMonitorState.reseller_id.is_(None),
    ).limit(1))
    if synced_at is None or stored_utc(synced_at) <= now - interval:
        return True
    due = await session.scalar(select(ConnectionMonitorTarget.id).where(
        ConnectionMonitorTarget.reseller_id.is_(None), ConnectionMonitorTarget.test_enabled.is_(True),
        ConnectionMonitorTarget.generation != "", ConnectionMonitorTarget.endpoint_count > 0,
        ConnectionMonitorTarget.next_check_at <= now,
        or_(ConnectionMonitorTarget.claim_until.is_(None), ConnectionMonitorTarget.claim_until <= now),
    ).limit(1))
    return due is not None


def plan_generation(plan: MonitorConfigPlan) -> str:
    if not plan.is_ready:
        return ""
    series = ":".join(sorted(endpoint.generation for endpoint in plan.endpoints))
    return hashlib.sha256(series.encode()).hexdigest()


def apply_target_plan(target: ConnectionMonitorTarget, plan: MonitorConfigPlan, *, now: datetime) -> bool:
    generation = plan_generation(plan)
    changed = target.generation != generation or target.endpoint_count != len(plan.endpoints)
    target.generation = generation
    target.endpoint_count = len(plan.endpoints)
    if not plan.is_ready:
        target.is_public = False
        target.claim_token = None
        target.claim_until = None
        if plan.issue in (MonitorIssue.HOST_UNAVAILABLE, MonitorIssue.CONFIG_UNAVAILABLE, MonitorIssue.OUT_OF_SCOPE):
            target.test_enabled = False
    if changed:
        target.claim_token = None
        target.claim_until = None
        target.next_check_at = now
    return changed


def initial_snapshots(target: ConnectionMonitorTarget, plan: MonitorConfigPlan) -> list[ConnectionMonitorSnapshot]:
    if plan.is_ready:
        return [ConnectionMonitorSnapshot(
            reseller_id=None, target_id=target.id, endpoint_key=endpoint.endpoint_key,
            probe_id=LOCAL_PROBE_ID, generation=target.generation,
        ) for endpoint in plan.endpoints]
    return [ConnectionMonitorSnapshot(
        reseller_id=None, target_id=target.id, endpoint_key="configuration", probe_id=LOCAL_PROBE_ID,
        generation="", status="not_ready", reason=plan.issue.value,
    )]


async def reconcile_monitor_plans(session: AsyncSession, cycle: MonitorCycleClaim,
                                  plans: tuple[MonitorConfigPlan, ...], *, now: datetime) -> bool:
    guard = await session.execute(update(ConnectionMonitorState).where(
        ConnectionMonitorState.id == cycle.state_id, ConnectionMonitorState.reseller_id.is_(None),
        current_monitor_cycle(cycle, now=now),
    ).values(catalog_synced_at=now))
    if guard.rowcount != 1:
        await session.rollback()
        return False
    targets = (await session.scalars(select(ConnectionMonitorTarget).where(
        ConnectionMonitorTarget.reseller_id.is_(None), ConnectionMonitorTarget.test_enabled.is_(True),
    ).order_by(ConnectionMonitorTarget.id).limit(MAX_MONITOR_ENDPOINTS + 1))).all()
    if len(targets) > MAX_MONITOR_ENDPOINTS:
        raise MonitorConfigError(MonitorIssue.CAPACITY)
    by_config = {plan.config_key: plan for plan in plans}
    target_ids = [target.id for target in targets]
    diagnostic_reasons: dict[int, str | None] = {}
    if target_ids:
        diagnostic_rows = (await session.execute(select(
            ConnectionMonitorSnapshot.target_id, ConnectionMonitorSnapshot.reason,
        ).where(
            ConnectionMonitorSnapshot.reseller_id.is_(None),
            ConnectionMonitorSnapshot.target_id.in_(target_ids),
            ConnectionMonitorSnapshot.endpoint_key == "configuration",
            ConnectionMonitorSnapshot.probe_id == LOCAL_PROBE_ID,
        ).limit(MAX_MONITOR_ENDPOINTS + 1))).all()
        diagnostic_reasons = {target_id: reason for target_id, reason in diagnostic_rows}
    changed_ids, snapshots = [], []
    for target in targets:
        plan = by_config.get(target.subscription_key, MonitorConfigPlan(target.subscription_key or "", issue=MonitorIssue.CONFIG_UNAVAILABLE))
        changed = apply_target_plan(target, plan, now=now)
        # Unsupported routes keep test_enabled; rebuild only when evidence is missing or drifted.
        if changed:
            should_rebuild = True
        elif not plan.is_ready:
            existing_reason = diagnostic_reasons.get(target.id)
            should_rebuild = existing_reason is None or existing_reason != (plan.issue.value if plan.issue else None)
        else:
            should_rebuild = False
        if should_rebuild:
            changed_ids.append(target.id)
            snapshots.extend(initial_snapshots(target, plan))
    if changed_ids:
        await session.execute(delete(ConnectionMonitorSnapshot).where(
            ConnectionMonitorSnapshot.reseller_id.is_(None), ConnectionMonitorSnapshot.target_id.in_(changed_ids),
        ))
        session.add_all(snapshots)
    await session.commit()
    return True
