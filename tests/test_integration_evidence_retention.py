"""Unacknowledged operational events hold older scoped management and security evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.db.integration_audit_models import IntegrationAdminAudit
from app.db.integration_security_models import IntegrationSecurityAudit
from app.db.integration_webhook_models import IntegrationEvent
from app.services.integration_admin_audit import prune_management_audit
from app.services.integration_events import append_event
from app.services.integration_management_forms import ManagementChange
from app.services.integration_security_retention import prune_security_records
from test_integration_management import ManagementDatabaseCase
from test_integration_foundation import READ_SCOPES


class IntegrationEvidenceRetentionTests(ManagementDatabaseCase):
    async def test_pending_events_retain_evidence_until_acknowledged(self) -> None:
        await self.execute(self.target("configure"), ManagementChange(status="active", scopes=READ_SCOPES, confirmed=True))
        old = datetime.now(timezone.utc)-timedelta(days=181)
        await self.session.execute(update(IntegrationAdminAudit).values(created_at=old))
        self.session.add(IntegrationSecurityAudit(id="synthetic-retention", request_id="req_synthetic", outcome="succeeded",
            integration_id=self.integration.id, owner_principal_id=11, scope_kind="shop", shop_id=11, occurrences=1,
            created_at=old, evidence_hold=False))
        await append_event(self.session, self.integration.id, event_type="operation.updated", resource_id="op_synthetic",
            customer_id="cus_synthetic", version=1, fields={"state": "needs_review"})
        await self.session.commit()
        self.assertEqual(await prune_management_audit(self.session), 0)
        self.assertEqual((await prune_security_records(self.session))[1], 0)
        await self.session.execute(update(IntegrationEvent).values(state="acknowledged"))
        await self.session.commit()
        self.assertEqual(await prune_management_audit(self.session), 1)
        self.assertEqual((await prune_security_records(self.session))[1], 1)
