"""Independent connection registration, revocation and backfill races on both databases."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Row

from app.db.customer_models import Customer, IntegrationCustomer
from app.db.integration_models import Integration
from app.db.models import BotUser, UserService
from app.services.customer_backfill import IdentityBatch, backfill_legacy_customers, backfill_legacy_services
from app.services.customer_identity import ensure_legacy_customer
from app.services.integration_customers import CustomerRegistration, CustomerInput, create_or_get_integration_customer as register
from app.services.integration_policy import IntegrationError
from test_integration_customers import database


@pytest.fixture(params=["sqlite", "postgresql"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[str, str | None]]:
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'identities.db'}", None
        return
    url = os.environ.get("PGCLOCK_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("PGCLOCK_TEST_POSTGRES_URL must select a disposable synthetic database")
    schema = f"customers_{uuid4().hex}"
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(sa.schema.CreateSchema(schema))
    try:
        yield url.replace("postgresql+psycopg:", "postgresql+asyncpg:"), schema
    finally:
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_registration_creates_exactly_one_pair(backend: tuple[str, str | None], conflict: bool) -> None:
    async def scenario() -> None:
        async with database(*backend) as (sessions, integration, key):
            gate = asyncio.Event()
            arrivals = 0
            async def create(name: str) -> CustomerRegistration | str:
                nonlocal arrivals
                async with sessions() as session:
                    arrivals += 1
                    if arrivals == 2:
                        gate.set()
                    await gate.wait()
                    if backend[0].startswith("sqlite"):
                        await session.execute(sa.text("BEGIN"))
                    try:
                        return await register(session, key.bearer, CustomerInput("same", name))
                    except IntegrationError as error:
                        return error.code
            results = await asyncio.gather(create("first"), create("second" if conflict else "first"))
            successful = [result for result in results if not isinstance(result, str)]
            assert sum(result.created for result in successful) == 1
            if conflict:
                assert results.count("customer_reference_conflict") == 1
            else:
                assert successful[0].customer == successful[1].customer
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Customer)) == 1
                assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationCustomer)) == 1
    asyncio.run(scenario())


def test_registration_rechecks_disable_after_waiting_for_management_lock(backend: tuple[str, str | None]) -> None:
    async def scenario() -> None:
        async with database(*backend) as (sessions, integration, key):
            async with sessions() as manager:
                await manager.execute(sa.update(Integration).where(Integration.id == integration).values(status="disabled"))
                async def create() -> None:
                    async with sessions() as session:
                        with pytest.raises(IntegrationError) as caught:
                            await register(session, key.bearer, CustomerInput("ref"))
                        assert caught.value.code == "integration_disabled"
                task = asyncio.create_task(create())
                await asyncio.sleep(0.05)
                await manager.commit()
                await task
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Customer)) == 0
    asyncio.run(scenario())


def test_backfill_and_legacy_adapter_races_preserve_single_adapter_and_public_ids(backend: tuple[str, str | None]) -> None:
    async def scenario() -> None:
        async with database(*backend) as (sessions, integration, key):
            async with sessions() as session:
                session.add(UserService(bot_user_id=11, pg_username="synthetic"))
                await session.flush()
                await session.execute(sa.update(UserService).values(public_id=None))
                await session.commit()
            async def customers() -> None:
                async with sessions() as session:
                    await backfill_legacy_customers(session)
                    await session.commit()
            async def adapter() -> None:
                async with sessions() as session:
                    await ensure_legacy_customer(session, 11)
                    await session.commit()
            await asyncio.gather(customers(), adapter())
            async def services() -> Row[tuple[int | None, str | None]]:
                async with sessions() as session:
                    await backfill_legacy_services(session)
                    await session.commit()
                    return (await session.execute(sa.select(UserService.customer_id, UserService.public_id))).one()
            first, second = await asyncio.gather(services(), services())
            assert first == second and first.customer_id and first.public_id.startswith("svc_")
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Customer)) == 1
                assert await session.scalar(sa.select(BotUser.telegram_id)) == 10011
    asyncio.run(scenario())


def test_concurrent_real_legacy_registration_has_no_orphan_adapter(backend: tuple[str, str | None]) -> None:
    from unittest.mock import AsyncMock, patch

    from app.services.users import get_or_create_user

    async def scenario() -> None:
        async with database(*backend) as (sessions, integration, key):
            async def create() -> int:
                async with sessions() as session:
                    return (await get_or_create_user(session, 90101)).id
            with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
                "app.services.customer_identity.integration_execution_active", return_value=True), patch(
                "app.services.users.get_settings") as settings:
                settings.return_value.admin_ids = []
                first, second = await asyncio.gather(create(), create())
            assert first == second
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(BotUser)) == 2
                assert await session.scalar(sa.select(sa.func.count()).select_from(Customer)) == 1
                assert await session.scalar(sa.select(Customer.legacy_bot_user_id)) == first
    asyncio.run(scenario())


def test_legacy_backfill_skips_website_services_and_resumes_past_them(backend: tuple[str, str | None]) -> None:
    async def scenario() -> None:
        async with database(*backend) as (sessions, integration, key), sessions() as session:
            legacy_customer = await ensure_legacy_customer(session, 11)
            web_customer = Customer()
            session.add(web_customer)
            await session.flush()
            session.add(IntegrationCustomer(integration_id=integration, customer_id=web_customer.id,
                external_customer_ref="synthetic-web"))
            session.add_all([
                UserService(id=50, bot_user_id=11, pg_username="legacy-before"),
                UserService(id=100, customer_id=web_customer.id, integration_id=integration,
                    origin_operation_id="op_synthetic", owner_principal_id=11, shop_id=11,
                    pg_user_id=701, pg_username="synthetic-web"),
                UserService(id=150, bot_user_id=11, pg_username="legacy-after"),
            ])
            await session.commit()
            query = sa.select(*UserService.__table__.c).where(UserService.id == 100)
            before = (await session.execute(query)).one()
            first = await backfill_legacy_services(session, limit=1)
            await session.commit()
            assert first == IdentityBatch(50, 1)
            second = await backfill_legacy_services(session, after=first.last_id, limit=1)
            await session.commit()
            assert second == IdentityBatch(150, 1)
            assert await backfill_legacy_services(session, after=second.last_id, limit=1) == IdentityBatch(150, 0)
            assert (await backfill_legacy_services(session)).scanned == 2
            await session.commit()
            assert (await session.execute(query)).one() == before
            assert list(await session.scalars(sa.select(UserService.customer_id).where(
                UserService.id.in_((50, 150))).order_by(UserService.id))) == [legacy_customer, legacy_customer]
    asyncio.run(scenario())
