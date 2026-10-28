"""Append allowlisted stable events in the resource's transaction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer
from app.db.integration_models import Integration
from app.db.integration_operation_models import IntegrationOperation
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.integration_webhook_models import IntegrationEvent, IntegrationLinkVersion, IntegrationWebhookDestination
from app.db.models import UserService
from app.services.integration_policy import ERROR_MESSAGES, IntegrationError
from app.services.integration_rate_limits import conflict_insert

EVENT_TYPES = frozenset({"operation.updated", "service.updated", "customer.telegram_link_changed"})


async def append_event(session: AsyncSession, integration_id: str, *, event_type: str, resource_id: str,
                       customer_id: str, version: int, fields: dict[str, str | bool | None]) -> None:
    if event_type not in EVENT_TYPES or type(version) is not int or version <= 0 or set(fields)-{"state", "failure_code", "linked"}:
        raise IntegrationError("invalid_request")
    if fields.get("failure_code") is not None and fields["failure_code"] not in ERROR_MESSAGES:
        raise IntegrationError("invalid_request")
    await session.execute(update(Integration).where(Integration.id == integration_id).values(revision=Integration.revision+1))
    now, event_id = datetime.now(timezone.utc), f"evt_{uuid4().hex}"
    destination_version = await session.scalar(select(IntegrationWebhookDestination.version).where(
        IntegrationWebhookDestination.integration_id == integration_id, IntegrationWebhookDestination.enabled.is_(True)))
    body = json.dumps({"event_id": event_id, "type": event_type, "occurred_at": now.isoformat(), "resource_id": resource_id,
        "customer_id": customer_id, "resource_version": version, **fields}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    await session.execute(conflict_insert(session, IntegrationEvent.__table__).values(integration_id=integration_id,
        event_id=event_id, type=event_type, resource_id=resource_id, customer_id=customer_id, resource_version=version,
        body=body, destination_version=destination_version, state="pending", attempts=0, next_attempt_at=now, created_at=now,
    ).on_conflict_do_nothing(index_elements=["integration_id", "type", "resource_id", "resource_version"]))


async def operation_event(session: AsyncSession, operation_id: int, state: str, version: int, code: str | None = None) -> None:
    row = (await session.execute(select(IntegrationOperation.integration_id, IntegrationOperation.public_id, Customer.public_id)
        .join(Customer, Customer.id == IntegrationOperation.customer_id).where(IntegrationOperation.id == operation_id))).one()
    await append_event(session, row[0], event_type="operation.updated", resource_id=row[1], customer_id=row[2], version=version,
        fields={"state": state, "failure_code": code})


async def service_event(session: AsyncSession, service_id: int) -> None:
    row = (await session.execute(select(UserService.integration_id, UserService.public_id, Customer.public_id,
        IntegrationServiceSnapshot.resource_version, IntegrationServiceSnapshot.state).join(Customer, Customer.id == UserService.customer_id)
        .join(IntegrationServiceSnapshot, IntegrationServiceSnapshot.service_id == UserService.id).where(
            UserService.id == service_id, UserService.integration_id.is_not(None)))).one()
    await append_event(session, row[0], event_type="service.updated", resource_id=row[1], customer_id=row[2], version=row[3], fields={"state": row[4]})


async def link_event(session: AsyncSession, integration_id: str, customer_id: int, *, linked: bool) -> None:
    table = IntegrationLinkVersion.__table__
    version = await session.scalar(conflict_insert(session, table).values(integration_id=integration_id, customer_id=customer_id, version=1)
        .on_conflict_do_update(index_elements=["integration_id", "customer_id"], set_={"version": table.c.version+1}).returning(table.c.version))
    public_id = await session.scalar(select(Customer.public_id).where(Customer.id == customer_id))
    await append_event(session, integration_id, event_type="customer.telegram_link_changed", resource_id=public_id,
        customer_id=public_id, version=version, fields={"linked": linked})
