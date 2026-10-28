"""Independent workers share durable external-reference and lease uniqueness."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db.integration_operation_models import IntegrationOperation
from app.services.integration_operation_claims import claim_operation
from app.services.integration_operations import submit_integration_operation
from test_customer_identity_concurrency import backend as backend
from test_integration_operations import operation_database


def test_concurrent_acceptance_and_claim_have_one_winner(backend):
    async def scenario():
        async with operation_database(*backend) as (sessions, integration, key, access, value, second):
            gate = asyncio.Event()
            arrived = 0
            async def submit(index):
                nonlocal arrived
                async with sessions() as session:
                    arrived += 1
                    if arrived == 2:
                        gate.set()
                    await gate.wait()
                    return await submit_integration_operation(session, access, value, idempotency_key=f"concurrent-key-{index}")
            results = await asyncio.gather(submit(1), submit(2))
            assert results[0].id == results[1].id
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(IntegrationOperation)) == 1
                operation_id = await session.scalar(select(IntegrationOperation.id))
            async def claim():
                async with sessions() as session:
                    return await claim_operation(session, operation_id)
            claims = await asyncio.gather(claim(), claim())
            assert sum(lease is not None for lease in claims) == 1
    asyncio.run(scenario())
