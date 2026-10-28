"""Execute one saved absolute target; uncertain dispatch is reconciled by reads only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.customer_models import Customer, IntegrationCustomer
from app.db.integration_delivery_models import IntegrationDelivery
from app.db.integration_models import Integration
from app.db.integration_operation_models import IntegrationCapacityCommitment, IntegrationOperation, IntegrationOperationAudit, ServiceMutationGuard
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.models import OrgPrincipal, UserService
from app.services.integration_addons import AddonTerms, accepted_addon, addon_target
from app.services.integration_authority import IntegrationAuthority, require_pg_evidence
from app.services.integration_catalog import ResolvedPlan
from app.services.integration_events import operation_event, service_event
from app.services.integration_execution_policy import ExecutionLimits, accepted_authority, assert_execution_capacity, current_plan
from app.services.integration_observations import MAX_DURATION_SECONDS, PanelIdentity, observe_service, strict_integer
from app.services.integration_operation_claims import OperationLease, claim_operation, transition_operation
from app.services.integration_operation_targets import PanelTarget, lease_condition, mark_dispatch_intent, mark_dispatch_sent, save_operation_target
from app.services.integration_operations import KIND_SCOPES
from app.services.integration_panel_mutations import DispatchPermit, dispatch_permit
from app.services.integration_pg import fetch_pg_evidence, integration_pg_client, principal_pg_staff
from app.services.plans_catalog import groups_allowed_for_staff
from app.services.integration_policy import IntegrationError
from app.services.integration_service_reads import observation_values
from app.services.integration_target_verification import panel_groups, validated_delivery_url, verify_target
from app.services.pasarguard import PasarGuardError, build_user_create_payload
from app.services.secret_box import encrypt_secret
from app.services.service_renewals import calculate_renewal

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    authority: IntegrationAuthority
    client: Any
    kind: str
    terms: dict
    service_id: int | None
    target: PanelTarget | None
    username: str | None
    panel_id: int | None
    public_operation: str


async def _context(session: AsyncSession, lease: OperationLease) -> ExecutionContext:
    operation = await session.scalar(select(IntegrationOperation).where(*lease_condition(lease)))
    if operation is None:
        raise IntegrationError("service_busy")
    authority = await accepted_authority(session, operation.integration_id, operation.kind)
    customer = await session.scalar(select(Customer.id).join(IntegrationCustomer, IntegrationCustomer.customer_id == Customer.id).where(
        Customer.id == operation.customer_id, Customer.state == "active", Customer.legacy_bot_user_id.is_(None),
        IntegrationCustomer.integration_id == operation.integration_id))
    if customer is None:
        raise IntegrationError("customer_blocked")
    service = await session.get(UserService, operation.service_id) if operation.service_id else None
    if service is not None and (service.integration_id != authority.integration_id or service.customer_id != customer or
            service.shop_id != authority.shop.shop_id or service.owner_principal_id != authority.owner.principal_id or
            service.bot_user_id is not None or service.shop_needs_review or service.is_cancelled or service.cancellation_pending):
        raise IntegrationError("not_found")
    target = PanelTarget(**json.loads(operation.target_json)) if operation.target_json else None
    return ExecutionContext(authority, await integration_pg_client(session, authority), operation.kind,
        json.loads(operation.accepted_terms_json), operation.service_id, target,
        service.pg_username if service else None, service.pg_user_id if service else None, operation.public_id)


def _new_target(context: ExecutionContext, terms: Any, info: dict | None) -> PanelTarget:
    now = int(datetime.now(timezone.utc).timestamp())
    owner = context.authority.owner.context.pg_admin_username or ""
    if context.kind == "provision":
        username = terms.username_pattern.format(prefix=terms.username_prefix, suffix=terms.username_suffix,
            random=secrets.token_hex(12))
        payload = build_user_create_payload(username=username, group_ids=list(terms.group_ids),
            data_limit=terms.data_limit_bytes or 0, expire_ts=now+terms.duration_seconds if terms.duration_seconds else 0,
            hwid_limit=terms.device_limit, note=context.public_operation)
        payload["hwid_limit"] = terms.device_limit or 0
        return PanelTarget("POST", "/api/user", payload, username, owner, None, context.public_operation)
    if info is None:
        raise IntegrationError("authorization_unavailable")
    observe_service(info, PanelIdentity(context.username or "", owner, context.panel_id))
    if context.kind == "addon":
        payload = addon_target(info, PanelIdentity(context.username or "", owner, context.panel_id), terms, now=now)
    else:
        result = calculate_renewal(info, {"volume_bytes": terms.data_limit_bytes, "duration_seconds": terms.duration_seconds or 0}, now=now)
        payload = result["payload"]
    strict_integer(payload["data_limit"])
    if payload.get("expire"):
        strict_integer(payload["expire"]-now, maximum=MAX_DURATION_SECONDS)
    strict_integer(info.get("hwid_limit") or 0, maximum=1000)
    payload.update(group_ids=list(panel_groups(info)), hwid_limit=info.get("hwid_limit") or 0, note=context.public_operation)
    return PanelTarget("PUT", f"/api/user/by-id/{context.panel_id}", payload, context.username or "", owner,
        context.panel_id, context.public_operation)


async def _current_terms(session: AsyncSession, authority: IntegrationAuthority, context: ExecutionContext,
                         admin: dict, groups: list) -> ResolvedPlan | AddonTerms:
    if context.kind == "addon":
        service = await session.get(UserService, context.service_id)
        operation_input = await session.scalar(select(IntegrationOperation.input_json).where(
            IntegrationOperation.public_id == context.public_operation, IntegrationOperation.integration_id == authority.integration_id))
        value = json.loads(operation_input)
        return await accepted_addon(session, authority, service, value["catalog_id"], value["terms_revision"])
    terms = await current_plan(session, authority, context.terms["local_id"], admin, groups)
    if terms.revision() != ResolvedPlan(**context.terms).revision():
        raise IntegrationError("terms_changed")
    return terms


async def _reserve_capacity(session: AsyncSession, lease: OperationLease, context: ExecutionContext,
                            authority: IntegrationAuthority, admin: dict, target: PanelTarget, info: dict | None) -> None:
    commitment = await session.scalar(select(IntegrationCapacityCommitment).where(IntegrationCapacityCommitment.operation_id == lease.operation_id))
    old_limit = (info or {}).get("data_limit") or 0
    delta = max(0, target.payload["data_limit"]-old_limit) if target.payload["data_limit"] else None
    if commitment is not None:
        # Exclude only this provably undispatched reservation while rechecking it.
        # Rollback restores it if a gate fails; no other transaction sees a release.
        commitment.active = False
        await session.flush()
    await assert_execution_capacity(session, ExecutionLimits(authority, admin, target.payload, delta, context.kind == "provision"))
    if commitment is None:
        session.add(IntegrationCapacityCommitment(operation_id=lease.operation_id, shop_id=authority.shop.shop_id,
            principal_id=authority.owner.principal_id, pg_username=target.owner.lower(), bytes=delta or 0, user_slots=int(context.kind == "provision")))
    else:
        commitment.active = True


async def _prepare(session: AsyncSession, lease: OperationLease, context: ExecutionContext, admin: dict,
                   groups: list, info: dict | None) -> PanelTarget:
    # Principal lock serializes commitments across integrations of the same actor.
    await session.execute(update(OrgPrincipal).where(OrgPrincipal.id == context.authority.owner.principal_id).values(
        status=OrgPrincipal.status))
    await session.execute(update(Integration).where(Integration.id == lease.integration_id).values(revision=Integration.revision+1))
    refreshed = await _context(session, lease)
    authority = refreshed.authority
    if authority.owner.principal_id != context.authority.owner.principal_id:
        raise IntegrationError("permission_denied")
    require_pg_evidence(authority, frozenset({KIND_SCOPES[context.kind], "services:read"}), context.authority.pg_evidence)
    terms = await _current_terms(session, authority, context, admin, groups)
    target = context.target or _new_target(context, terms, info)
    if context.target is not None and context.target.marker != context.public_operation:
        raise IntegrationError("service_identity_mismatch")
    if not groups_allowed_for_staff(principal_pg_staff(authority, admin), target.payload["group_ids"]):
        raise IntegrationError("permission_denied")
    await _reserve_capacity(session, lease, context, authority, admin, target, info)
    operation = await session.scalar(select(IntegrationOperation).where(*lease_condition(lease)))
    if operation is None:
        raise IntegrationError("service_busy")
    if operation.service_id is None:
        service = UserService(integration_id=authority.integration_id, customer_id=operation.customer_id,
            origin_operation_id=operation.public_id, owner_principal_id=authority.owner.principal_id,
            shop_id=authority.shop.shop_id, pg_username=target.username, plan_id=terms.local_id)
        session.add(service)
        await session.flush()
        operation.service_id = service.id
        session.add_all([IntegrationServiceSnapshot(service_id=service.id), ServiceMutationGuard(service_id=service.id, operation_id=operation.id)])
    await save_operation_target(session, lease, target, baseline={"panel_id": info["id"], "used_bytes": info["used_traffic"], "limit_bytes": info["data_limit"],
        "status": info["status"], "expire": info["expire"]} if info else None)
    await session.flush()
    if context.service_id is None:
        await service_event(session, operation.service_id)
    await session.commit()
    return target


async def _seal(session: AsyncSession, lease: OperationLease, target: PanelTarget, payload: dict,
                *, panel_url: str, subscription_path: str, reconcile: bool = False) -> None:
    observation = verify_target(payload, target)
    url = validated_delivery_url(payload, panel_url=panel_url, subscription_path=subscription_path)
    encrypted = await asyncio.to_thread(encrypt_secret, url)
    if not encrypted:
        raise IntegrationError("access_not_ready")
    await session.execute(update(Integration).where(Integration.id == lease.integration_id).values(revision=Integration.revision+1))
    operation = await session.scalar(select(IntegrationOperation).where(*lease_condition(lease, states=("outcome_unknown", "needs_review") if reconcile else ("running",))))
    if operation is None or operation.service_id is None:
        raise IntegrationError("service_busy")
    service = await session.get(UserService, operation.service_id)
    if service.integration_id != lease.integration_id or service.customer_id != operation.customer_id or service.bot_user_id is not None:
        raise IntegrationError("service_identity_mismatch")
    now = datetime.now(timezone.utc)
    snapshot = await session.get(IntegrationServiceSnapshot, service.id)
    for key, value in observation_values(observation, now).items():
        setattr(snapshot, key, value)
    snapshot.state = "ready"
    snapshot.resource_version += 1
    if operation.kind == "renew":
        service.plan_id = json.loads(operation.accepted_terms_json)["local_id"]
    service.pg_user_id = observation.panel_id
    service.quota_data_limit_bytes = observation.limit_bytes or 0
    service.quota_expire_at = observation.expires_at
    service.quota_status = observation.status
    service.quota_synced_at = now
    delivery = await session.get(IntegrationDelivery, service.id)
    digest = hashlib.sha256(url.encode()).hexdigest()
    if delivery is not None and delivery.url_digest != digest:
        raise IntegrationError("service_identity_mismatch")
    if delivery is None:
        session.add(IntegrationDelivery(service_id=service.id, url_enc=encrypted, url_digest=digest, validated_at=now))
    else:
        delivery.validated_at = now
    if reconcile:
        await _complete_reconciliation(session, lease)
    else:
        await transition_operation(session, lease, "succeeded")
    await service_event(session, service.id)
    await session.commit()


async def _complete_reconciliation(session: AsyncSession, lease: OperationLease) -> None:
    version = await session.scalar(update(IntegrationOperation).where(*lease_condition(lease,
        states=("outcome_unknown", "needs_review"))).values(state="succeeded", failure_code=None, evidence_hold=False,
        lease_token=None, lease_expires_at=None, resource_version=IntegrationOperation.resource_version+1,
        updated_at=datetime.now(timezone.utc)).returning(IntegrationOperation.resource_version).execution_options(synchronize_session=False))
    if version is None:
        raise IntegrationError("service_busy")
    session.add(IntegrationOperationAudit(operation_id=lease.operation_id, kind="state", state="succeeded", version=version))
    await session.execute(update(IntegrationCapacityCommitment).where(IntegrationCapacityCommitment.operation_id == lease.operation_id).values(active=False))
    await session.execute(delete(ServiceMutationGuard).where(ServiceMutationGuard.operation_id == lease.operation_id))
    await operation_event(session, lease.operation_id, "succeeded", version)


async def _record_failure(sessions: async_sessionmaker[AsyncSession], lease: OperationLease, error: BaseException) -> None:
    async with sessions() as session:
        await session.execute(update(Integration).where(Integration.id == lease.integration_id).values(revision=Integration.revision+1))
        operation = await session.scalar(select(IntegrationOperation).where(*lease_condition(lease)))
        if operation is None:
            return
        transient = isinstance(error, (httpx.HTTPError, TimeoutError, PasarGuardError)) or (
            isinstance(error, IntegrationError) and error.code in {"authorization_unavailable", "panel_unavailable", "temporarily_unavailable"})
        if operation.dispatch_intent_at is None:
            state = "queued" if transient else "failed"
        else:
            state = "needs_review" if isinstance(error, IntegrationError) else "outcome_unknown"
        code = error.code if isinstance(error, IntegrationError) else "temporarily_unavailable" if state in {"failed", "queued"} else "outcome_unknown"
        await transition_operation(session, lease, state, code=code)
        if operation.service_id is not None and state != "queued":
            await session.execute(update(IntegrationServiceSnapshot).where(IntegrationServiceSnapshot.service_id == operation.service_id).values(
                state="cancelled" if state == "failed" and operation.kind == "provision" else "review",
                resource_version=IntegrationServiceSnapshot.resource_version+1))
            await service_event(session, operation.service_id)
        await session.commit()


async def execute_integration_operation(sessions: async_sessionmaker[AsyncSession], operation_id: int,
                                        *, panel_url: str | None = None, subscription_path: str | None = None) -> bool:
    async with sessions() as session:
        lease = await claim_operation(session, operation_id)
    if lease is None:
        return False
    try:
        async with sessions() as session:
            context = await _context(session, lease)
        evidence = await fetch_pg_evidence(context.authority, sessions, allow_stale=False)
        context = replace(context, authority=replace(context.authority, pg_evidence=evidence))
        async with asyncio.timeout(10):
            admin, groups = await asyncio.gather(context.client.request("GET", "/api/admin", use_cache=False), context.client.get_groups_simple())
            info = await context.client.request("GET", f"/api/user/by-id/{context.panel_id}", use_cache=False) if context.panel_id else None
        async with sessions() as session:
            target = await _prepare(session, lease, context, admin, groups, info)
            await mark_dispatch_intent(session, lease)
            # This is the last local permission boundary. It cannot retract a sent request.
            await accepted_authority(session, lease.integration_id, context.kind)
            await mark_dispatch_sent(session, lease)
        context.client._integration_sessions = sessions
        context.client._integration_fences_enabled = True
        permit = DispatchPermit(lease, target, context.client._read_cache_ident())
        with dispatch_permit(permit):
            async with asyncio.timeout(20):
                await context.client.request(target.method, target.path, json=target.payload)
        async with asyncio.timeout(5):
            payload = await context.client.request("GET", f"/api/user/by-username/{target.username}", use_cache=False)
        settings = get_settings()
        async with sessions() as session:
            await _seal(session, lease, target, payload, panel_url=panel_url or settings.pg_base_url,
                subscription_path=subscription_path or settings.pg_subscription_path)
        return True
    except BaseException as error:
        logger.warning("Integration execution stopped operation=%s type=%s", operation_id, type(error).__name__)
        await _record_failure(sessions, lease, error)
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return False
