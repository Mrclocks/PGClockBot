"""Accept one immutable technical intent under permanent external-reference uniqueness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer
from app.services.integration_addons import accepted_addon
from app.services.integration_authority import IntegrationAuthority
from app.db.integration_operation_models import IntegrationOperation, IntegrationOperationAudit, ServiceMutationGuard
from app.db.integration_read_models import IntegrationCatalogPlan
from app.db.models import Plan, ResellerSetting, Setting, UserService
from app.db.integration_models import Integration
from app.services.integration_catalog import NAMING_KEYS, ResolvedPlan, materialize_plan
from app.services.billing import BillingError, assert_billing_allows_provision
from app.services.integration_credentials import verify_credential
from app.services.integration_customers import REFERENCE_PATTERN
from app.services.integration_operation_views import OperationArchived, OperationView, operation_view
from app.services.integration_policy import IntegrationError
from app.services.integration_replays import RequestBinding, bind_replay, lookup_replay
from app.services.integration_request_keys import canonical_digest, request_key_digest
from app.services.integration_service_reads import ReadAccess, mapped_customer, read_authority, scoped_services
from app.services.integrations import eligible_plans_query

KIND_SCOPES = {"provision": "services:provision", "renew": "services:renew", "addon": "services:addons"}
OPEN_STATES = ("queued", "running")
UNCERTAIN_STATES = ("outcome_unknown", "needs_review")
QUEUE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    customer_id: str
    external_operation_ref: str
    catalog_id: str
    terms_revision: str
    kind: str = "provision"
    service_id: str | None = None


def execution_binding(value: ExecutionRequest) -> RequestBinding:
    if value.kind not in KIND_SCOPES or not isinstance(value.external_operation_ref, str) or not REFERENCE_PATTERN.fullmatch(value.external_operation_ref):
        raise IntegrationError("invalid_request")
    if not isinstance(value.terms_revision, str) or len(value.terms_revision) != 64 or any(c not in "0123456789abcdef" for c in value.terms_revision):
        raise IntegrationError("invalid_request")
    if (value.kind == "provision") != (value.service_id is None):
        raise IntegrationError("invalid_request")
    return RequestBinding("POST", value.kind, value.customer_id, asdict(value))


async def _accepted_plan(session: AsyncSession, authority: IntegrationAuthority, value: ExecutionRequest) -> str:
    row = (await session.execute(select(IntegrationCatalogPlan, Plan).join(Plan, Plan.id == IntegrationCatalogPlan.plan_id).where(
        IntegrationCatalogPlan.integration_id == authority.integration_id, IntegrationCatalogPlan.public_id == value.catalog_id,
        Plan.id.in_(authority.allowed_plan_ids), Plan.id.in_(eligible_plans_query(authority.shop)),
    ))).one_or_none()
    if row is None or row[0].terms_json is None:
        raise IntegrationError("plan_ineligible")
    if row[0].terms_revision != value.terms_revision:
        raise IntegrationError("terms_changed")
    saved = ResolvedPlan(**json.loads(row[0].terms_json))
    query = select(Setting.key, Setting.value).where(Setting.key.in_(NAMING_KEYS)) if authority.shop.shop_id is None else select(
        ResellerSetting.key, ResellerSetting.value).where(ResellerSetting.reseller_user_id == authority.shop.shop_id,
            ResellerSetting.key.in_(NAMING_KEYS))
    naming = dict((await session.execute(query)).all())
    try:
        current = materialize_plan(row[1], naming, frozenset(saved.group_ids), device_limit=saved.device_limit)
    except IntegrationError:
        raise IntegrationError("terms_changed") from None
    if current.revision() != value.terms_revision:
        raise IntegrationError("terms_changed")
    return row[0].terms_json


async def _existing_intent(session: AsyncSession, authority: IntegrationAuthority, customer: Customer, value: ExecutionRequest,
                           key: str, binding: RequestBinding) -> IntegrationOperation | None:
    await lookup_replay(session, authority, key, binding)
    digest = hashlib.sha256(value.external_operation_ref.encode("ascii")).hexdigest()
    row = await session.scalar(select(IntegrationOperation).where(
        IntegrationOperation.integration_id == authority.integration_id, IntegrationOperation.external_ref_digest == digest,
    ).execution_options(populate_existing=True))
    if row is not None and (row.customer_id != customer.id or row.input_digest != canonical_digest(asdict(binding))):
        raise IntegrationError("external_reference_conflict")
    return row


async def _queue_available(session: AsyncSession, integration_id: str) -> None:
    counts = dict((await session.execute(select(IntegrationOperation.state, func.count()).where(
        IntegrationOperation.integration_id == integration_id,
        IntegrationOperation.state.in_((*OPEN_STATES, *UNCERTAIN_STATES)),
    ).group_by(IntegrationOperation.state))).all())
    if sum(counts.get(state, 0) for state in OPEN_STATES) >= QUEUE_LIMIT or sum(counts.get(state, 0) for state in UNCERTAIN_STATES) >= QUEUE_LIMIT:
        raise IntegrationError("queue_limit_reached")


async def _mutation_service(session: AsyncSession, authority: IntegrationAuthority, customer: Customer,
                            value: ExecutionRequest) -> UserService | None:
    if value.service_id is None:
        return None
    row = (await session.execute(scoped_services(authority, customer.id).where(UserService.public_id == value.service_id)
        .with_for_update(of=UserService))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    service, snapshot, _catalog = row
    if snapshot is None or snapshot.state != "ready" or service.is_cancelled or service.cancellation_pending:
        raise IntegrationError("access_not_ready")
    if await session.get(ServiceMutationGuard, service.id) is not None:
        raise IntegrationError("service_busy")
    return service


async def submit_integration_operation(session: AsyncSession, access: ReadAccess, value: ExecutionRequest,
                                        *, idempotency_key: str) -> OperationView:
    request_key_digest(idempotency_key)
    binding = execution_binding(value)
    try:
        await verify_credential(session, access.bearer, lock_integration=True)
        authority = await read_authority(session, access, frozenset({KIND_SCOPES[value.kind], "operations:read"}))
        customer = await mapped_customer(session, authority, value.customer_id)
        existing = await _existing_intent(session, authority, customer, value, idempotency_key, binding)
        if existing is None:
            policy = await session.scalar(select(Integration.customer_entry_policy).where(Integration.id == authority.integration_id))
            if policy != "website":
                raise IntegrationError("website_entry_not_enabled")
            await _queue_available(session, authority.integration_id)
            service = await _mutation_service(session, authority, customer, value)
            terms = json.dumps(asdict(await accepted_addon(session, authority, service, value.catalog_id, value.terms_revision)), sort_keys=True) if value.kind == "addon" else await _accepted_plan(session, authority, value)
            await assert_billing_allows_provision(session, reseller_user_id=authority.shop.shop_id, suspend=False)
            now = datetime.now(timezone.utc)
            existing = IntegrationOperation(integration_id=authority.integration_id, customer_id=customer.id,
                service_id=service.id if service else None, accepting_credential_selector=authority.credential_selector,
                external_ref=value.external_operation_ref, external_ref_digest=hashlib.sha256(value.external_operation_ref.encode("ascii")).hexdigest(),
                input_digest=canonical_digest(asdict(binding)), input_json=json.dumps(asdict(value), sort_keys=True),
                accepted_terms_json=terms, kind=value.kind, created_at=now, updated_at=now)
            session.add(existing)
            await session.flush()
            if service is not None:
                session.add(ServiceMutationGuard(service_id=service.id, operation_id=existing.id))
            session.add(IntegrationOperationAudit(operation_id=existing.id, kind="state", state="queued", version=1))
        await bind_replay(session, authority, idempotency_key, binding, resource_kind="operation", resource_id=existing.public_id)
        public_service = await session.scalar(select(UserService.public_id).where(UserService.id == existing.service_id)) if existing.service_id else None
        if existing.archived_at is not None:
            await session.commit()
            raise OperationArchived(existing.public_id)
        result = operation_view(existing, value.customer_id, public_service)
        await session.commit()
        return result
    except BillingError:
        await session.rollback()
        raise IntegrationError("billing_blocked") from None
    except BaseException:
        await session.rollback()
        raise
