"""Operation projections exclude execution targets, panel identities and delivery credentials."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_operation_models import IntegrationOperation
from app.db.models import UserService
from app.services.integration_policy import ERROR_MESSAGES, IntegrationError, utc_time
from app.services.integration_service_reads import ReadAccess, mapped_customer, read_authority


@dataclass(frozen=True, slots=True)
class OperationFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class OperationView:
    id: str
    customer_id: str
    external_operation_ref: str
    kind: str
    state: str
    service_id: str | None
    resource_version: int
    created_at: datetime
    updated_at: datetime
    next_poll_after_seconds: int
    failure: OperationFailure | None


class OperationArchived(IntegrationError):
    def __init__(self, operation_id: str) -> None:
        super().__init__("operation_archived")
        self.operation_id = operation_id


def operation_view(operation: IntegrationOperation, customer_id: str, service_id: str | None) -> OperationView:
    if operation.archived_at is not None:
        raise OperationArchived(operation.public_id)
    failure = OperationFailure(operation.failure_code, ERROR_MESSAGES.get(operation.failure_code, ERROR_MESSAGES["internal_error"])) if operation.failure_code else None
    return OperationView(operation.public_id, customer_id, operation.external_ref, operation.kind, operation.state,
        service_id, operation.resource_version, utc_time(operation.created_at), utc_time(operation.updated_at), 5, failure)


async def read_integration_operation(session: AsyncSession, access: ReadAccess, customer_id: str, operation_id: str) -> OperationView:
    authority = await read_authority(session, access, frozenset({"operations:read"}))
    customer = await mapped_customer(session, authority, customer_id)
    row = (await session.execute(select(IntegrationOperation, UserService.public_id).outerjoin(
        UserService, UserService.id == IntegrationOperation.service_id,
    ).where(IntegrationOperation.integration_id == authority.integration_id, IntegrationOperation.customer_id == customer.id,
            IntegrationOperation.public_id == operation_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    return operation_view(row[0], customer_id, row[1])
