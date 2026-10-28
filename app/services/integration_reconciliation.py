"""Scoped operators verify saved effects with reads; no forced retry or failure release."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.integration_operation_models import IntegrationOperation, IntegrationOperationAudit
from app.services.authz import can_shop
from app.services.integration_authority import require_pg_evidence
from app.services.integration_execution import _seal
from app.services.integration_execution_policy import accepted_authority
from app.services.integration_operation_claims import OperationLease
from app.services.integration_operation_targets import PanelTarget
from app.services.integration_operations import KIND_SCOPES
from app.services.integration_pg import fetch_pg_evidence, integration_pg_client
from app.services.integration_policy import IntegrationError, SCOPE_FEATURES
from app.services.integrations import managed_integration


async def claim_reconciliation(session: AsyncSession, staff: dict, integration_id: str, operation_id: str) -> OperationLease:
    integration, owner = await managed_integration(session, staff, integration_id, lock=True)
    if any(not can_shop(owner.context, permission) for permission in ("integration_operations_reconcile", "integration_operations_read", "integration_services_read")):
        raise IntegrationError("permission_denied")
    row = await session.scalar(select(IntegrationOperation).where(IntegrationOperation.integration_id == integration.id,
        IntegrationOperation.public_id == operation_id))
    if row is None:
        raise IntegrationError("not_found")
    if any(not can_shop(owner.context, permission) for permission in SCOPE_FEATURES[KIND_SCOPES[row.kind]]):
        raise IntegrationError("permission_denied")
    now, token = datetime.now(timezone.utc), secrets.token_hex(32)
    version = await session.scalar(update(IntegrationOperation).where(IntegrationOperation.id == row.id,
        IntegrationOperation.integration_id == integration.id, IntegrationOperation.state.in_(("outcome_unknown", "needs_review")),
        IntegrationOperation.target_json.is_not(None), IntegrationOperation.dispatch_intent_at.is_not(None),
        or_(IntegrationOperation.lease_token.is_(None), IntegrationOperation.lease_expires_at <= now),
    ).values(lease_token=token, lease_expires_at=now+timedelta(seconds=60), resource_version=IntegrationOperation.resource_version+1)
        .returning(IntegrationOperation.resource_version).execution_options(synchronize_session=False))
    if version is None:
        raise IntegrationError("service_busy")
    session.add(IntegrationOperationAudit(operation_id=row.id, kind="reconciliation_read", state=row.state, version=version,
        actor_principal_id=owner.principal_id))
    await session.commit()
    return OperationLease(row.id, integration.id, token, version)


async def reconcile_saved_effect(sessions: async_sessionmaker[AsyncSession], lease: OperationLease,
                                 *, panel_url: str | None = None, subscription_path: str | None = None) -> bool:
    async with sessions() as session:
        row = await session.scalar(select(IntegrationOperation).where(IntegrationOperation.id == lease.operation_id,
            IntegrationOperation.integration_id == lease.integration_id, IntegrationOperation.lease_token == lease.token,
            IntegrationOperation.resource_version == lease.version, IntegrationOperation.state.in_(("outcome_unknown", "needs_review"))))
        if row is None or row.target_json is None:
            raise IntegrationError("service_busy")
        authority = await accepted_authority(session, row.integration_id, row.kind, execution=False)
        target = PanelTarget(**json.loads(row.target_json))
        client = await integration_pg_client(session, authority)
    evidence = await fetch_pg_evidence(authority, sessions, allow_stale=False)
    require_pg_evidence(authority, frozenset({"services:read"}), evidence)
    async with asyncio.timeout(5):
        payload = await client.request("GET", f"/api/user/by-username/{target.username}", use_cache=False)
    settings = get_settings()
    async with sessions() as session:
        await _seal(session, lease, target, payload, panel_url=panel_url or settings.pg_base_url,
            subscription_path=subscription_path or settings.pg_subscription_path, reconcile=True)
    return True
