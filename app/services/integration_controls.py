"""Replay-protected scoped operational controls and secret-free operator projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer
from app.db.integration_operation_models import IntegrationNativeMutation, IntegrationOperation
from app.db.integration_webhook_models import IntegrationControlAction, IntegrationEvent, IntegrationWebhookDestination
from app.db.models import ServiceAddonPack, UserService
from app.services.authz import can_shop
from app.services.integration_addons import set_addon_grants
from app.services.integration_owners import IntegrationOwner
from app.services.integration_policy import IntegrationError
from app.services.integration_rate_limits import conflict_insert
from app.services.integration_reconciliation import claim_reconciliation
from app.services.integration_webhooks import WebhookIssuance, configure_destination, rotate_webhook_key
from app.services.integration_operation_claims import OperationLease
from app.services.integrations import managed_integration

CONTROL_PERMISSIONS = {"packs": ("plans", "integration_services_addons"), "destination": ("integration_webhooks_manage",),
    "signing_key": ("integration_webhooks_manage",), "redeliver": ("integration_webhooks_manage",),
    "reconcile": ("integration_operations_reconcile",)}


def control_signer(secret: bytes) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="pgclock-integration-controls")


def control_ticket(signer: URLSafeTimedSerializer, owner: IntegrationOwner, integration_id: str, action: str, resource_id: str = "") -> str:
    if action not in CONTROL_PERMISSIONS:
        raise IntegrationError("invalid_request")
    return signer.dumps([uuid4().hex, owner.principal_id, owner.shop.kind.value, owner.shop.shop_id, integration_id, action, resource_id])


async def _claim_control(session: AsyncSession, staff: dict, integration_id: str, action: str, resource_id: str,
                         signer: URLSafeTimedSerializer, ticket: str) -> IntegrationOwner:
    integration, owner = await managed_integration(session, staff, integration_id, lock=True)
    if action not in CONTROL_PERMISSIONS or any(not can_shop(owner.context, permission) for permission in CONTROL_PERMISSIONS[action]):
        raise IntegrationError("permission_denied")
    try:
        values = signer.loads(ticket, max_age=1200)
        expected = [owner.principal_id, owner.shop.kind.value, owner.shop.shop_id, integration.id, action, resource_id]
        if not isinstance(values, list) or len(values) != 7 or values[1:] != expected or len(values[0]) != 32:
            raise ValueError
    except (BadData, ValueError, TypeError):
        raise IntegrationError("invalid_request") from None
    result = await session.execute(conflict_insert(session, IntegrationControlAction.__table__).values(
        id=values[0], integration_id=integration.id, actor_principal_id=owner.principal_id,
        kind=action, resource_id=resource_id or None).on_conflict_do_nothing(index_elements=["id"]))
    if result.rowcount != 1:
        raise IntegrationError("already_submitted")
    return owner


@dataclass(frozen=True, slots=True)
class ControlChange:
    action: str
    resource_id: str = ""
    url: str = ""
    enabled: bool = False
    pack_ids: frozenset[int] = frozenset()
    overlap_minutes: int = 0
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class ControlResult:
    issuance: WebhookIssuance | None = None
    reconciliation: OperationLease | None = None


async def execute_control(session: AsyncSession, staff: dict, integration_id: str, change: ControlChange,
                          *, signer: URLSafeTimedSerializer, ticket: str) -> ControlResult:
    try:
        if change.confirmed is not True or change.action not in CONTROL_PERMISSIONS:
            raise IntegrationError("confirmation_required")
        if change.action == "destination":
            # DNS validation ends its read transaction before awaiting the resolver.
            await configure_destination(session, staff, integration_id, change.url, enabled=change.enabled)
        await _claim_control(session, staff, integration_id, change.action, change.resource_id, signer, ticket)
        result = await _apply_control(session, staff, integration_id, change)
        await session.commit()
        return result
    except BaseException:
        await session.rollback()
        raise


async def _apply_control(session: AsyncSession, staff: dict, integration_id: str, change: ControlChange) -> ControlResult:
    if change.action == "packs":
        await set_addon_grants(session, staff, integration_id, change.pack_ids)
    elif change.action == "signing_key":
        return ControlResult(issuance=await rotate_webhook_key(session, staff, integration_id, overlap_minutes=change.overlap_minutes))
    elif change.action == "redeliver":
        await _redeliver(session, integration_id, change.resource_id)
    elif change.action == "reconcile":
        return ControlResult(reconciliation=await claim_reconciliation(session, staff, integration_id, change.resource_id))
    return ControlResult()


async def _redeliver(session: AsyncSession, integration_id: str, public_id: str) -> None:
    destination = await session.get(IntegrationWebhookDestination, integration_id)
    if destination is None or not destination.enabled:
        raise IntegrationError("invalid_request")
    result = await session.execute(update(IntegrationEvent).where(IntegrationEvent.integration_id == integration_id,
        IntegrationEvent.event_id == public_id, IntegrationEvent.state != "acknowledged",
        IntegrationEvent.state != "delivering").values(state="pending", attempts=0, next_attempt_at=datetime.now(timezone.utc),
            destination_version=destination.version, lease_token=None, lease_expires_at=None))
    if result.rowcount != 1:
        raise IntegrationError("service_busy")


@dataclass(frozen=True, slots=True)
class OperationSummary:
    id: str
    customer_id: str
    kind: str
    state: str
    version: int
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class EventSummary:
    id: str
    type: str
    state: str
    attempts: int
    last_status: int | None


@dataclass(frozen=True, slots=True)
class NativeSummary:
    service_id: str
    state: str


@dataclass(frozen=True, slots=True)
class ControlPage:
    owner: IntegrationOwner
    operations: tuple[OperationSummary, ...]
    events: tuple[EventSummary, ...]
    packs: tuple[tuple[int, str], ...]
    audit: tuple[tuple[str, str | None], ...]
    actions: frozenset[str]
    next_operation: int | None
    next_event: int | None
    native: tuple[NativeSummary, ...]
    next_native: int | None


async def control_page(session: AsyncSession, staff: dict, integration_id: str, *, before_operation: int = 0,
                        before_event: int = 0, before_native: int = 0) -> ControlPage:
    integration, owner = await managed_integration(session, staff, integration_id)
    if not can_shop(owner.context, "integration_operations_read"):
        raise IntegrationError("permission_denied")
    actions = frozenset(action for action, permissions in CONTROL_PERMISSIONS.items()
        if all(can_shop(owner.context, permission) for permission in permissions))
    operations = (await session.execute(select(IntegrationOperation, Customer.public_id).join(Customer,
        Customer.id == IntegrationOperation.customer_id).where(IntegrationOperation.integration_id == integration.id,
        IntegrationOperation.id < before_operation if before_operation else True).order_by(IntegrationOperation.id.desc()).limit(25))).all()
    events = list(await session.scalars(select(IntegrationEvent).where(IntegrationEvent.integration_id == integration.id,
        IntegrationEvent.id < before_event if before_event else True).order_by(IntegrationEvent.id.desc()).limit(25)))
    packs = (await session.execute(select(ServiceAddonPack.id, ServiceAddonPack.name).where(
        ServiceAddonPack.owner_reseller_id == owner.shop.shop_id, ServiceAddonPack.is_active.is_(True)).order_by(ServiceAddonPack.id).limit(25))).all()
    audit = (await session.execute(select(IntegrationControlAction.kind, IntegrationControlAction.resource_id).where(
        IntegrationControlAction.integration_id == integration.id).order_by(IntegrationControlAction.created_at.desc(),
            IntegrationControlAction.id.desc()).limit(25))).all()
    native = (await session.execute(select(IntegrationNativeMutation.id, UserService.public_id, IntegrationNativeMutation.state)
        .join(UserService, UserService.id == IntegrationNativeMutation.service_id).where(
            IntegrationNativeMutation.integration_id == integration.id, UserService.integration_id == integration.id,
            UserService.shop_id == owner.shop.shop_id, UserService.owner_principal_id == owner.principal_id,
            IntegrationNativeMutation.state != "completed", IntegrationNativeMutation.id < before_native if before_native else True)
        .order_by(IntegrationNativeMutation.id.desc()).limit(25))).all()
    return ControlPage(owner, tuple(OperationSummary(row.public_id, public, row.kind, row.state, row.resource_version, row.failure_code)
        for row, public in operations), tuple(EventSummary(row.event_id, row.type, row.state, row.attempts, row.last_status) for row in events),
        tuple(tuple(row) for row in packs), tuple(tuple(row) for row in audit), actions,
        operations[-1][0].id if len(operations) == 25 else None, events[-1].id if len(events) == 25 else None, tuple(NativeSummary(public, state) for _id, public, state in native),
        native[-1][0] if len(native) == 25 else None)
