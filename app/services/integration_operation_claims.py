"""Lease claims and conservative recovery never treat an expired lease as a stopped request."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_models import Integration
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.customer_models import Customer, IntegrationCustomer
from app.db.integration_operation_models import IntegrationCapacityCommitment, IntegrationOperation, IntegrationOperationAudit, ServiceMutationGuard
from app.services.integration_events import operation_event, service_event
from app.services.integration_policy import ERROR_MESSAGES, IntegrationError

LEASE_TTL = timedelta(seconds=60)
QUEUE_TTL = timedelta(hours=24)
TRANSITIONS = {
    "queued": frozenset({"running", "failed"}), "running": frozenset({"queued", "succeeded", "failed", "outcome_unknown", "needs_review"}),
    "outcome_unknown": frozenset({"succeeded", "failed", "needs_review"}), "needs_review": frozenset({"succeeded", "failed"}),
}


@dataclass(frozen=True, slots=True)
class OperationLease:
    operation_id: int
    integration_id: str
    token: str
    version: int


async def claim_operation(session: AsyncSession, operation_id: int, *, now: datetime | None = None) -> OperationLease | None:
    now = now or datetime.now(timezone.utc)
    integration_id = await session.scalar(update(Integration).where(Integration.id.in_(
        select(IntegrationOperation.integration_id).where(IntegrationOperation.id == operation_id),
    )).values(revision=Integration.revision+1).returning(Integration.id))
    if integration_id is None:
        await session.rollback()
        return None
    running = await session.scalar(select(func.count()).select_from(IntegrationOperation).where(
        IntegrationOperation.integration_id == integration_id, IntegrationOperation.state == "running"))
    if running >= 2:
        await session.rollback()
        return None
    token = secrets.token_hex(32)
    result = await session.execute(update(IntegrationOperation).where(
        IntegrationOperation.id == operation_id, IntegrationOperation.integration_id == integration_id,
        IntegrationOperation.state == "queued", IntegrationOperation.created_at > now-QUEUE_TTL,
        or_(IntegrationOperation.failure_code.is_(None), IntegrationOperation.updated_at <= now-timedelta(seconds=60)),
        IntegrationOperation.customer_id.in_(select(IntegrationCustomer.customer_id).join(
            Customer, Customer.id == IntegrationCustomer.customer_id,
        ).where(IntegrationCustomer.integration_id == integration_id, Customer.state == "active",
                Customer.legacy_bot_user_id.is_(None))),
    ).values(state="running", failure_code=None, lease_token=token, lease_expires_at=now+LEASE_TTL, updated_at=now,
             resource_version=IntegrationOperation.resource_version+1).returning(IntegrationOperation.resource_version)
        .execution_options(synchronize_session=False))
    version = result.scalar_one_or_none()
    if version is None:
        await session.rollback()
        return None
    session.add(IntegrationOperationAudit(operation_id=operation_id, kind="state", state="running", version=version))
    await operation_event(session, operation_id, "running", version)
    await session.commit()
    return OperationLease(operation_id, integration_id, token, version)


async def heartbeat_operation(session: AsyncSession, lease: OperationLease, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    result = await session.execute(update(IntegrationOperation).where(
        IntegrationOperation.id == lease.operation_id, IntegrationOperation.integration_id == lease.integration_id,
        IntegrationOperation.state == "running", IntegrationOperation.lease_token == lease.token,
        IntegrationOperation.lease_expires_at > now,
    ).values(lease_expires_at=now+LEASE_TTL).execution_options(synchronize_session=False))
    await session.commit()
    return result.rowcount == 1


async def transition_operation(session: AsyncSession, lease: OperationLease, state: str, *, code: str | None = None,
                               now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    await session.execute(update(Integration).where(Integration.id == lease.integration_id).values(revision=Integration.revision+1))
    if state not in TRANSITIONS["running"] or code is not None and code not in ERROR_MESSAGES:
        raise IntegrationError("invalid_request")
    condition = [IntegrationOperation.id == lease.operation_id, IntegrationOperation.integration_id == lease.integration_id,
        IntegrationOperation.state == "running", IntegrationOperation.lease_token == lease.token,
        IntegrationOperation.resource_version == lease.version, IntegrationOperation.lease_expires_at > now]
    if state in {"queued", "failed"}:
        condition.append(IntegrationOperation.dispatch_intent_at.is_(None))
    terminal = state in {"succeeded", "failed"}
    result = await session.execute(update(IntegrationOperation).where(*condition).values(
        state=state, failure_code=code, resource_version=IntegrationOperation.resource_version+1,
        updated_at=now, lease_token=None, lease_expires_at=None, evidence_hold=not terminal,
    ).returning(IntegrationOperation.resource_version).execution_options(synchronize_session=False))
    version = result.scalar_one_or_none()
    if version is None:
        raise IntegrationError("service_busy")
    session.add(IntegrationOperationAudit(operation_id=lease.operation_id, kind="state", state=state, version=version, code=code))
    if terminal:
        await session.execute(update(IntegrationCapacityCommitment).where(
            IntegrationCapacityCommitment.operation_id == lease.operation_id,
        ).values(active=False).execution_options(synchronize_session=False))
        await session.execute(delete(ServiceMutationGuard).where(ServiceMutationGuard.operation_id == lease.operation_id))
    await operation_event(session, lease.operation_id, state, version, code)
    return version


async def recover_expired_operation(session: AsyncSession, operation_id: int, *, now: datetime | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    await session.execute(update(Integration).where(Integration.id.in_(select(IntegrationOperation.integration_id)
        .where(IntegrationOperation.id == operation_id))).values(revision=Integration.revision+1))
    # Intent commits before any write. Recovery fences the old worker locally;
    # it cannot retract a request already sent to the panel.
    row = (await session.execute(select(IntegrationOperation.integration_id, IntegrationOperation.dispatch_intent_at).where(
        IntegrationOperation.id == operation_id, IntegrationOperation.state == "running",
        IntegrationOperation.lease_expires_at <= now,
    ))).one_or_none()
    if row is None:
        return None
    state = "outcome_unknown" if row.dispatch_intent_at else "queued"
    condition = IntegrationOperation.dispatch_intent_at.is_not(None) if row.dispatch_intent_at else IntegrationOperation.dispatch_intent_at.is_(None)
    result = await session.execute(update(IntegrationOperation).where(
        IntegrationOperation.id == operation_id, IntegrationOperation.integration_id == row.integration_id,
        IntegrationOperation.state == "running", IntegrationOperation.lease_expires_at <= now, condition,
    ).values(state=state, resource_version=IntegrationOperation.resource_version+1,
             lease_token=None, lease_expires_at=None, updated_at=now).returning(IntegrationOperation.resource_version)
        .execution_options(synchronize_session=False))
    version = result.scalar_one_or_none()
    if version is not None:
        session.add(IntegrationOperationAudit(operation_id=operation_id, kind="state", state=state, version=version))
        await operation_event(session, operation_id, state, version)
    await session.commit()
    return state if version is not None else None


async def expire_queued_operation(session: AsyncSession, operation_id: int, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    await session.execute(update(Integration).where(Integration.id.in_(select(IntegrationOperation.integration_id)
        .where(IntegrationOperation.id == operation_id))).values(revision=Integration.revision+1))
    result = await session.execute(update(IntegrationOperation).where(
        IntegrationOperation.id == operation_id, IntegrationOperation.state == "queued",
        IntegrationOperation.created_at <= now-QUEUE_TTL, IntegrationOperation.dispatch_intent_at.is_(None),
    ).values(state="failed", failure_code="queue_expired", evidence_hold=False, updated_at=now,
             resource_version=IntegrationOperation.resource_version+1).returning(IntegrationOperation.resource_version)
        .execution_options(synchronize_session=False))
    version = result.scalar_one_or_none()
    if version is not None:
        session.add(IntegrationOperationAudit(operation_id=operation_id, kind="state", state="failed", version=version, code="queue_expired"))
        await session.execute(update(IntegrationCapacityCommitment).where(
            IntegrationCapacityCommitment.operation_id == operation_id,
        ).values(active=False).execution_options(synchronize_session=False))
        await session.execute(delete(ServiceMutationGuard).where(ServiceMutationGuard.operation_id == operation_id))
        await operation_event(session, operation_id, "failed", version, "queue_expired")
        cancelled = await session.scalar(update(IntegrationServiceSnapshot).where(IntegrationServiceSnapshot.service_id.in_(
            select(IntegrationOperation.service_id).where(IntegrationOperation.id == operation_id, IntegrationOperation.kind == "provision")),
            IntegrationServiceSnapshot.state == "provisioning").values(state="cancelled",
                resource_version=IntegrationServiceSnapshot.resource_version+1).returning(IntegrationServiceSnapshot.service_id)
            .execution_options(synchronize_session=False))
        if cancelled is not None:
            await service_event(session, cancelled)
    await session.commit()
    return version is not None
