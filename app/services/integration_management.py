"""Atomically claim management forms, mutate foundation state and append an audit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_audit_models import IntegrationManagementAction
from app.services.integration_admin_audit import AuditState, append_admin_audit, audit_state
from app.services.integration_credentials import CredentialIssuance, issue_credential, read_credential, revoke_credential, rotate_credential
from app.services.integration_management_forms import (
    ManagementAction, ManagementChange, ManagementTarget, read_management_ticket, validate_change,
)
from app.services.integration_owners import IntegrationOwner, management_owner
from app.services.integration_policy import IntegrationError
from app.services.integrations import configure_integration, create_integration, managed_integration, set_integration_plans


@dataclass(frozen=True, slots=True)
class ManagementResult:
    integration_id: str
    issuance: CredentialIssuance | None = field(default=None, repr=False)


async def _claim_action(
    session: AsyncSession, owner: IntegrationOwner, target: ManagementTarget, action_id: str,
) -> IntegrationManagementAction:
    table = IntegrationManagementAction.__table__
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    result = await session.execute(insert(table).values(
        id=action_id, actor_principal_id=owner.principal_id, scope_kind=owner.shop.kind.value,
        shop_id=owner.shop.shop_id, integration_id=target.integration_id, action=target.action.value,
    ).on_conflict_do_nothing(index_elements=["id"]))
    if result.rowcount != 1:
        raise IntegrationError("already_submitted")
    action = await session.get(IntegrationManagementAction, action_id)
    assert action is not None
    return action


async def _change_integration(
    session: AsyncSession, staff: dict, target: ManagementTarget, change: ManagementChange,
) -> ManagementResult:
    if target.action == ManagementAction.CREATE:
        row = await create_integration(session, staff, grants=change.scopes, commit=False)
        return ManagementResult(row.id)
    assert target.integration_id is not None
    if target.action == ManagementAction.CONFIGURE:
        await configure_integration(session, staff, target.integration_id, status=change.status,
                                    grants=change.scopes, customer_entry_policy=change.customer_entry_policy, commit=False)
    else:
        await set_integration_plans(session, staff, target.integration_id, plan_ids=change.plan_ids, commit=False)
    return ManagementResult(target.integration_id)


async def _change_credential(
    session: AsyncSession, staff: dict, target: ManagementTarget, change: ManagementChange,
) -> ManagementResult:
    assert target.integration_id is not None
    expiry = datetime.now(timezone.utc) + timedelta(days=change.lifetime_days)
    issuance = None
    if target.action == ManagementAction.ISSUE:
        issuance = await issue_credential(session, staff, target.integration_id, scopes=change.scopes,
                                           expires_at=expiry, commit=False)
    elif target.action == ManagementAction.ROTATE:
        issuance = await rotate_credential(session, staff, target.integration_id, target.selector,
                                            overlap=timedelta(minutes=change.overlap_minutes), expires_at=expiry, commit=False)
    else:
        await revoke_credential(session, staff, target.integration_id, target.selector, commit=False)
    return ManagementResult(target.integration_id, issuance)


async def _apply_change(
    session: AsyncSession, staff: dict, target: ManagementTarget, change: ManagementChange | None,
) -> ManagementResult:
    if change is None:
        raise IntegrationError("invalid_request")
    validate_change(target, change)
    if target.action in {ManagementAction.CREATE, ManagementAction.CONFIGURE, ManagementAction.PLANS}:
        return await _change_integration(session, staff, target, change)
    return await _change_credential(session, staff, target, change)


async def _before_change(session: AsyncSession, staff: dict, target: ManagementTarget) -> AuditState:
    if target.integration_id is None:
        return AuditState()
    row, _ = await managed_integration(session, staff, target.integration_id, lock=True)
    if target.action in {ManagementAction.CONFIGURE, ManagementAction.PLANS}:
        return await audit_state(session, row, plans=target.action == ManagementAction.PLANS)
    return AuditState()


async def _after_change(session: AsyncSession, staff: dict, target: ManagementTarget, result: ManagementResult) -> AuditState:
    if target.action in {ManagementAction.CREATE, ManagementAction.CONFIGURE, ManagementAction.PLANS}:
        row, _ = await managed_integration(session, staff, result.integration_id)
        return await audit_state(session, row, plans=target.action == ManagementAction.PLANS)
    return AuditState()


async def _mutate_and_audit(
    session: AsyncSession, staff: dict, target: ManagementTarget, change: ManagementChange | None,
    action: IntegrationManagementAction,
) -> tuple[ManagementResult | None, IntegrationError | None]:
    before = await _before_change(session, staff, target)
    result, error = None, None
    try:
        async with session.begin_nested():
            result = await _apply_change(session, staff, target, change)
    except IntegrationError as exc:
        error = exc
    if result is not None:
        action.integration_id = result.integration_id
        after = await _after_change(session, staff, target, result)
    else:
        after = before
    selector = result.issuance.credential.selector if result and result.issuance else target.selector or None
    await append_admin_audit(session, action, before=before, after=after, error=error, selector=selector)
    return result, error


async def execute_management(
    session: AsyncSession, staff: dict, target: ManagementTarget, change: ManagementChange | None, *,
    signer: URLSafeTimedSerializer, ticket: str,
) -> ManagementResult:
    """Own this unit of work; default-committing foundation callers are unchanged."""
    try:
        owner = await management_owner(session, staff)
        action_id = read_management_ticket(signer, ticket, owner, target)
        if target.integration_id is not None:
            await managed_integration(session, staff, target.integration_id)
        if target.selector:
            await read_credential(session, staff, target.integration_id, target.selector)
        action = await _claim_action(session, owner, target, action_id)
        result, error = await _mutate_and_audit(session, staff, target, change, action)
        await session.commit()
    except BaseException:
        # Cancellation or audit/storage failure must not leave a committed mutation.
        await session.rollback()
        raise
    if error:
        raise error
    assert result is not None
    return result
