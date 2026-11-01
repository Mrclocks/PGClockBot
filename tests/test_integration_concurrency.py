"""Credential rotation races on independent SQLite and PostgreSQL connections."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.db.integration_models import IntegrationCredential
from app.db.models import BotUser, OrgPrincipal, OrgPrincipalWebIdentity, ResellerProfile, Setting
from app.services.integration_authority import resolve_integration_authority
from app.services.integration_credentials import CredentialIssuance, issue_credential, revoke_credential, rotate_credential
from app.services.integration_policy import IntegrationError
from app.services.integrations import configure_integration, create_integration

STAFF = {"role": "principal", "web_identity_id": 11}


async def seed(session: AsyncSession) -> str:
    session.add(BotUser(id=11, telegram_id=10011, referral_code="synthetic-race"))
    await session.flush()
    session.add(ResellerProfile(
        id=11, user_id=11, pg_admin_username="pg-race", is_active=True,
        setup_completed_at=datetime.now(timezone.utc), web_username="race", web_password_hash="synthetic-hash",
        web_permissions="integrations_manage,integration_customers_read",
    ))
    await session.flush()
    session.add(OrgPrincipal(id=1, depth=0, status="active"))
    await session.flush()
    session.add(OrgPrincipal(id=11, parent_id=1, depth=1, status="active", reseller_profile_id=11,
                             bot_user_id=11, pg_username="pg-race", pg_api_key_enc="synthetic-ciphertext"))
    await session.flush()
    session.add(OrgPrincipalWebIdentity(id=11, principal_id=11, is_active=True, web_username="race",
                                       web_password_hash="synthetic-hash"))
    session.add(Setting(key="integration_api_enabled", value="1"))
    await session.commit()
    scopes = frozenset({"customers:read"})
    integration = await create_integration(session, STAFF, grants=scopes)
    await configure_integration(session, STAFF, integration.id, status="active", grants=scopes)
    return integration.id


async def rotation_race(url: str, schema: str | None) -> None:
    options = {"connect_args": {"server_settings": {"search_path": schema}}} if schema else {}
    engine = create_async_engine(url, **options)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            integration_id = await seed(session)
            key = await issue_credential(session, STAFF, integration_id, scopes=frozenset({"customers:read"}))
        entered = asyncio.Event()
        count = 0

        async def rotate() -> CredentialIssuance | str:
            nonlocal count
            async with sessions() as session:
                count += 1
                if count == 2:
                    entered.set()
                await entered.wait()
                try:
                    return await rotate_credential(session, STAFF, integration_id, key.credential.selector,
                                                   overlap=timedelta(minutes=5))
                except IntegrationError as error:
                    return error.code

        results = await asyncio.gather(rotate(), rotate())
        assert sum(result == "invalid_credential" for result in results) == 1
        replacement = next(result for result in results if not isinstance(result, str))
        async with sessions() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationCredential)) == 2
            await resolve_integration_authority(session, key.bearer)
            await resolve_integration_authority(session, replacement.bearer)
            async with sessions() as other:
                await revoke_credential(other, STAFF, integration_id, replacement.credential.selector)
            with pytest.raises(IntegrationError) as caught:
                await resolve_integration_authority(session, replacement.bearer)
            assert caught.value.code == "invalid_credential"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_two_connections_rotate_once_and_see_committed_revocation(dialect: str, tmp_path: Path) -> None:
    if dialect == "sqlite":
        asyncio.run(rotation_race(f"sqlite+aiosqlite:///{tmp_path / 'rotation.db'}", None))
        return
    url = os.environ.get("PGCLOCK_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("PGCLOCK_TEST_POSTGRES_URL must select an isolated synthetic test database")
    schema = f"phase1a_race_{uuid4().hex}"
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(sa.schema.CreateSchema(schema))
    try:
        asyncio.run(rotation_race(url.replace("postgresql+psycopg:", "postgresql+asyncpg:"), schema))
    finally:
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()
