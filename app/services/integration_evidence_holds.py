"""Unresolved effects and unacknowledged events retain their integration's evidence."""

from __future__ import annotations

from sqlalchemy import Select, select, union

from app.db.integration_operation_models import IntegrationNativeMutation, IntegrationOperation
from app.db.integration_webhook_models import IntegrationEvent


def held_integration_ids() -> Select:
    rows = union(select(IntegrationOperation.integration_id).where(IntegrationOperation.evidence_hold.is_(True)),
        select(IntegrationNativeMutation.integration_id).where(IntegrationNativeMutation.state != "completed"),
        select(IntegrationEvent.integration_id).where(IntegrationEvent.state != "acknowledged")).subquery()
    return select(rows.c.integration_id)
