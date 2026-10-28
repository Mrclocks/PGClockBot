"""Allowlisted administrative audit snapshots, scoped history and bounded retention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_audit_models import IntegrationAdminAudit, IntegrationManagementAction
from app.db.integration_models import Integration, IntegrationPlan
from app.services.integration_owners import management_owner
from app.services.integration_evidence_holds import held_integration_ids
from app.services.integration_policy import IntegrationError, stored_scopes
from app.services.integrations import managed_integration

AUDIT_RETENTION = timedelta(days=180)
AUDIT_PAGE_SIZE = 25
RETENTION_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class AuditState:
    status: str | None = None
    grants: tuple[str, ...] = ()
    plan_ids: tuple[int, ...] | None = None
    customer_entry_policy: str | None = None

    def values(self) -> dict:
        if self.status is not None:
            if self.status not in {"active", "disabled", "retired"}:
                raise IntegrationError("invalid_request")
            stored_scopes(",".join(self.grants))
            values = {"status": self.status, "grants": list(self.grants)}
            if self.customer_entry_policy is not None:
                if self.customer_entry_policy not in {"denied", "website"}:
                    raise IntegrationError("invalid_request")
                values["customer_entry_policy"] = self.customer_entry_policy
            return values
        if self.plan_ids is not None:
            if len(self.plan_ids) > 100 or any(type(value) is not int or not 0 < value <= 2**31-1 for value in self.plan_ids):
                raise IntegrationError("invalid_request")
            return {"plan_ids": list(self.plan_ids)}
        return {}


async def audit_state(session: AsyncSession, row: Integration, *, plans: bool = False) -> AuditState:
    if plans:
        ids = await session.scalars(select(IntegrationPlan.plan_id).where(
            IntegrationPlan.integration_id == row.id,
        ).order_by(IntegrationPlan.plan_id).limit(100))
        return AuditState(plan_ids=tuple(ids))
    return AuditState(status=row.status, grants=tuple(sorted(stored_scopes(row.grants))),
                      customer_entry_policy=row.customer_entry_policy)


async def append_admin_audit(
    session: AsyncSession, action: IntegrationManagementAction, *, before: AuditState,
    after: AuditState, error: IntegrationError | None = None, selector: str | None = None,
) -> None:
    session.add(IntegrationAdminAudit(
        action_id=action.id, actor_principal_id=action.actor_principal_id,
        scope_kind=action.scope_kind, shop_id=action.shop_id, integration_id=action.integration_id,
        action=action.action, outcome="denied" if error else "succeeded",
        error_code=error.code if error else None, credential_selector=selector,
        before=before.values(), after=after.values(), created_at=datetime.now(timezone.utc),
    ))
    await session.flush()


def validate_audit_cursor(before: int) -> None:
    if type(before) is not int or not 0 <= before <= 2**31-1:
        raise IntegrationError("invalid_request")


async def management_audit_page(
    session: AsyncSession, staff: dict, integration_id: str | None = None, *, before: int = 0,
) -> list[IntegrationAdminAudit]:
    validate_audit_cursor(before)
    if integration_id is None:
        owner = await management_owner(session, staff)
    else:
        _, owner = await managed_integration(session, staff, integration_id)
    query = select(IntegrationAdminAudit).where(
        IntegrationAdminAudit.actor_principal_id == owner.principal_id,
        IntegrationAdminAudit.scope_kind == owner.shop.kind.value,
        IntegrationAdminAudit.shop_id == owner.shop.shop_id,
    )
    if integration_id is not None:
        query = query.where(IntegrationAdminAudit.integration_id == integration_id)
    if before:
        query = query.where(IntegrationAdminAudit.id < before)
    return list(await session.scalars(query.order_by(IntegrationAdminAudit.id.desc()).limit(AUDIT_PAGE_SIZE)))


async def prune_management_audit(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Keep held evidence and permanent action tombstones even after history expires."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise IntegrationError("invalid_request")
    cutoff = now - AUDIT_RETENTION
    eligible = select(IntegrationAdminAudit.id).where(
        IntegrationAdminAudit.evidence_hold.is_(False), IntegrationAdminAudit.created_at < cutoff,
        (IntegrationAdminAudit.integration_id.is_(None) | ~IntegrationAdminAudit.integration_id.in_(held_integration_ids())),
    ).order_by(IntegrationAdminAudit.created_at, IntegrationAdminAudit.id).limit(RETENTION_BATCH_SIZE)
    ids = list(await session.scalars(eligible))
    if not ids:
        await session.rollback()
        return 0
    result = await session.execute(delete(IntegrationAdminAudit).where(
        IntegrationAdminAudit.id.in_(ids), IntegrationAdminAudit.evidence_hold.is_(False),
        IntegrationAdminAudit.created_at < cutoff,
        (IntegrationAdminAudit.integration_id.is_(None) | ~IntegrationAdminAudit.integration_id.in_(held_integration_ids())),
    ))
    await session.commit()
    return result.rowcount
