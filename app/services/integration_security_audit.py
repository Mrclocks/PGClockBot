"""Bound authentication evidence independently from transactional management history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_models import Integration
from app.db.integration_security_models import IntegrationSecurityAudit, SECURITY_OUTCOMES
from app.services.integration_credentials import verify_credential
from app.services.integration_policy import IntegrationError
from app.services.integration_rate_limits import conflict_insert, rate_window, take_slot


@dataclass(frozen=True, slots=True)
class VerifiedIntegration:
    integration_id: str
    owner_principal_id: int
    scope_kind: str
    shop_id: int | None


async def verify_audit_identity(session: AsyncSession, bearer: str) -> VerifiedIntegration:
    credential = await verify_credential(session, bearer)
    row = await session.scalar(select(Integration).where(Integration.id == credential.integration_id))
    if row is None:
        raise IntegrationError("invalid_credential")
    return VerifiedIntegration(row.id, row.owner_principal_id, row.scope_kind, row.shop_id)


async def record_security_outcome(
    session: AsyncSession, request_id: str, outcome: str, *,
    identity: VerifiedIntegration | None = None, ip_digest: str | None = None,
) -> None:
    if outcome not in SECURITY_OUTCOMES:
        raise IntegrationError("invalid_request")
    window = rate_window()
    detailed = await take_slot(session, "global", "audit", window)
    values = {
        "id": uuid4().hex if detailed else f"overflow:{window.minute}:{outcome}",
        "request_id": request_id, "outcome": outcome, "created_at": datetime.now(timezone.utc),
        "occurrences": 1,
    }
    if detailed:
        values["ip_digest"] = ip_digest
        if identity is not None:
            values.update(integration_id=identity.integration_id, owner_principal_id=identity.owner_principal_id,
                          scope_kind=identity.scope_kind, shop_id=identity.shop_id)
    table = IntegrationSecurityAudit.__table__
    # Overflow stores one anonymous aggregate per fixed outcome/minute, with one sample correlation ID.
    await session.execute(conflict_insert(session, table).values(**values).on_conflict_do_update(
        index_elements=[table.c.id], set_={"occurrences": table.c.occurrences + 1},
    ))
