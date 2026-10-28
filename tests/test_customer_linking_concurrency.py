"""Independent database connections resolve challenge and active-link races."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db.customer_link_models import CustomerLinkChallenge, CustomerTelegramLink
from app.services.customer_linking import complete_link_challenge, start_link_challenge
from app.services.integration_policy import IntegrationError
from test_customer_identity_concurrency import backend as backend
from test_customer_linking import linking_database, telegram_proof
from app.services.integration_customers import CustomerInput, create_or_get_integration_customer


def test_concurrent_challenge_key_returns_one_secret_and_one_record(backend) -> None:
    async def scenario():
        async with linking_database(*backend) as (sessions, integration, key, customer):
            gate = asyncio.Event()
            arrived = 0

            async def start():
                nonlocal arrived
                async with sessions() as session:
                    arrived += 1
                    if arrived == 2:
                        gate.set()
                    await gate.wait()
                    return await start_link_challenge(session, key.bearer, customer.id, idempotency_key="concurrent-create")

            results = await asyncio.gather(start(), start())
            assert len({row.challenge_id for row in results}) == 1
            assert sum(row.start_token is not None for row in results) == 1
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(CustomerLinkChallenge)) == 1
    asyncio.run(scenario())


def test_concurrent_customer_claims_never_reassign_the_same_telegram_account(backend) -> None:
    async def scenario():
        async with linking_database(*backend) as (sessions, integration, key, first):
            async with sessions() as session:
                second = (await create_or_get_integration_customer(session, key.bearer, CustomerInput("second"))).customer
                challenges = []
                for index, customer in enumerate((first, second)):
                    challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key=f"start-customer-{index}")
                    await telegram_proof(session, challenge)
                    challenges.append((customer, challenge))
            gate = asyncio.Event()
            arrived = 0

            async def finish(customer, challenge, index):
                nonlocal arrived
                async with sessions() as session:
                    arrived += 1
                    if arrived == 2:
                        gate.set()
                    await gate.wait()
                    try:
                        await complete_link_challenge(session, key.bearer, customer.id, challenge.challenge_id,
                            idempotency_key=f"finish-customer-{index}", confirm=True)
                        return "linked"
                    except IntegrationError as error:
                        return error.code

            results = await asyncio.gather(*(finish(customer, challenge, index) for index, (customer, challenge) in enumerate(challenges)))
            assert sorted(results) == ["link_conflict", "linked"]
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(CustomerTelegramLink)) == 1
                assert await session.scalar(select(func.count()).select_from(CustomerLinkChallenge).where(
                    CustomerLinkChallenge.consumed_at.is_not(None))) == 1
    asyncio.run(scenario())
