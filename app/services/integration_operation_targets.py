"""Commit immutable absolute targets and dispatch markers before an external write."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_operation_models import IntegrationOperation, IntegrationOperationAudit
from app.services.integration_operation_claims import OperationLease
from app.services.integration_policy import IntegrationError
from app.services.integration_runtime import retain_execution_history


@dataclass(frozen=True, slots=True)
class PanelTarget:
    method: str
    path: str
    payload: dict
    username: str
    owner: str
    panel_id: int | None
    marker: str

    def serialized(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)


def lease_condition(lease: OperationLease, *, states: tuple[str, ...] = ("running",)) -> tuple[ColumnElement, ...]:
    return (
        IntegrationOperation.id == lease.operation_id, IntegrationOperation.integration_id == lease.integration_id,
        IntegrationOperation.state.in_(states), IntegrationOperation.lease_token == lease.token,
        IntegrationOperation.resource_version == lease.version,
        IntegrationOperation.lease_expires_at > datetime.now(timezone.utc),
    )


async def save_operation_target(session: AsyncSession, lease: OperationLease, target: PanelTarget,
                                 *, baseline: dict | None = None) -> None:
    await retain_execution_history(session)
    result = await session.execute(update(IntegrationOperation).where(
        *lease_condition(lease), IntegrationOperation.target_json.is_(None), IntegrationOperation.dispatch_intent_at.is_(None),
    ).values(target_json=target.serialized(), baseline_json=json.dumps(baseline, sort_keys=True) if baseline is not None else None)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        existing = await session.scalar(select(IntegrationOperation.target_json).where(*lease_condition(lease)))
        if existing != target.serialized():
            raise IntegrationError("service_busy")
        return
    session.add(IntegrationOperationAudit(operation_id=lease.operation_id, kind="target_saved", state="running", version=lease.version))


async def mark_dispatch_intent(session: AsyncSession, lease: OperationLease) -> None:
    result = await session.execute(update(IntegrationOperation).where(*lease_condition(lease),
        IntegrationOperation.target_json.is_not(None), IntegrationOperation.dispatch_intent_at.is_(None),
    ).values(dispatch_intent_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise IntegrationError("service_busy")
    session.add(IntegrationOperationAudit(operation_id=lease.operation_id, kind="dispatch_intent", state="running", version=lease.version))
    await session.commit()


async def mark_dispatch_sent(session: AsyncSession, lease: OperationLease) -> None:
    result = await session.execute(update(IntegrationOperation).where(*lease_condition(lease),
        IntegrationOperation.dispatch_intent_at.is_not(None), IntegrationOperation.dispatch_sent_at.is_(None),
    ).values(dispatch_sent_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise IntegrationError("service_busy")
    session.add(IntegrationOperationAudit(operation_id=lease.operation_id, kind="dispatch_sent", state="running", version=lease.version))
    await session.commit()
