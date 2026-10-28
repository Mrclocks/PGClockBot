"""Independent connections share event version barriers and one dispatch lease."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.db.integration_webhook_models import IntegrationEvent, IntegrationWebhookDestination
from app.services.integration_events import append_event
from app.services.integration_webhooks import claim_event
from test_customer_identity_concurrency import backend as backend
from test_integration_service_reads import read_database


def test_shared_outbox_identity_and_lease_prevent_parallel_attempts(backend):
    async def scenario():
        async with read_database(*backend) as (sessions, integration, key, first, second, plan, access):
            async with sessions() as session:
                session.add(IntegrationWebhookDestination(integration_id=integration, url="https://hooks.example.com/events", enabled=True))
                await session.commit()
            async def append():
                async with sessions() as session:
                    await append_event(session, integration, event_type="operation.updated", resource_id="op_synthetic",
                        customer_id=first.id, version=1, fields={"state": "queued"})
                    await session.commit()
            await asyncio.gather(append(), append())
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(IntegrationEvent)) == 1
                internal = await session.scalar(select(IntegrationEvent.id))
            now = datetime.now(timezone.utc)
            async def claim():
                async with sessions() as session:
                    return await claim_event(session, internal, now=now)
            leases = await asyncio.gather(claim(), claim())
            assert sum(lease is not None for lease in leases) == 1
            async with sessions() as session:
                await session.execute(update(IntegrationEvent).where(IntegrationEvent.id == internal).values(attempts=13,
                    lease_expires_at=now-timedelta(seconds=1)))
                await session.commit()
                assert await claim_event(session, internal, now=now) is None
                assert (await session.get(IntegrationEvent, internal, populate_existing=True)).state == "exhausted"
    asyncio.run(scenario())
