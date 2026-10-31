"""Bounded counter and authentication-evidence retention with evidence-hold rechecks."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_security_models import IntegrationRateCounter, IntegrationSecurityAudit
from app.services.integration_admin_audit import AUDIT_RETENTION, RETENTION_BATCH_SIZE
from app.services.integration_rate_limits import COUNTER_RETENTION_MINUTES, rate_window


async def prune_security_records(session: AsyncSession, *, now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    cutoff_window = rate_window(now).minute - COUNTER_RETENTION_MINUTES
    counters = IntegrationRateCounter
    keys = select(counters.subject, counters.budget, counters.window).where(
        counters.window < cutoff_window,
    ).order_by(counters.window, counters.subject, counters.budget).limit(RETENTION_BATCH_SIZE)
    counter_result = await session.execute(delete(counters).where(
        tuple_(counters.subject, counters.budget, counters.window).in_(keys), counters.window < cutoff_window,
    ).execution_options(synchronize_session=False))
    audit = IntegrationSecurityAudit
    cutoff = now - AUDIT_RETENTION
    ids = select(audit.id).where(audit.evidence_hold.is_(False), audit.created_at < cutoff).order_by(
        audit.created_at, audit.id,
    ).limit(RETENTION_BATCH_SIZE)
    audit_result = await session.execute(delete(audit).where(
        audit.id.in_(ids), audit.evidence_hold.is_(False), audit.created_at < cutoff,
    ).execution_options(synchronize_session=False))
    await session.commit()
    return counter_result.rowcount, audit_result.rowcount
