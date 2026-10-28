"""Independent SQLite/PostgreSQL transactions claim management forms and serialize credential changes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import test_integration_concurrency as foundation
from app.db import Base
from app.db.integration_audit_models import IntegrationAdminAudit, IntegrationManagementAction
from app.db.integration_models import Integration, IntegrationCredential
from app.services.integration_credentials import issue_credential
from app.services.integration_management import ManagementResult, execute_management
from app.services.integration_management_forms import ManagementAction, ManagementChange, ManagementTarget, management_signer, new_management_ticket
from app.services.integration_owners import management_owner
from app.services.integration_policy import IntegrationError


async def management_race(url: str, schema: str | None, kind: str) -> None:
    options = {"connect_args": {"server_settings": {"search_path": schema}}} if schema else {}
    engine = create_async_engine(url, **options)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            integration_id = await foundation.seed(session)
            key = await issue_credential(session, foundation.STAFF, integration_id, scopes=frozenset({"customers:read"}))
            owner = await management_owner(session, foundation.STAFF)
        action = ManagementAction.ROTATE if kind in {"rotate", "rotate_revoke", "audit_failure"} else ManagementAction(kind)
        target = ManagementTarget(action, None if action == ManagementAction.CREATE else integration_id,
                                  key.credential.selector if action == ManagementAction.ROTATE else "")
        signer = management_signer(b"synthetic-race-secret")
        ticket = new_management_ticket(signer, owner, target)
        entered = asyncio.Event()
        arrived = 0

        async def run(target: ManagementTarget, ticket: str) -> ManagementResult | str:
            nonlocal arrived
            async with sessions() as session:
                arrived += 1
                if arrived == 2:
                    entered.set()
                await entered.wait()
                try:
                    return await execute_management(session, foundation.STAFF, target, ManagementChange(confirmed=True), signer=signer, ticket=ticket)
                except IntegrationError as error:
                    return error.code

        if kind == "audit_failure":
            async with sessions() as session:
                with patch("app.services.integration_management.append_admin_audit", AsyncMock(side_effect=SQLAlchemyError("synthetic fault"))):
                    with pytest.raises(SQLAlchemyError):
                        await execute_management(session, foundation.STAFF, target, ManagementChange(confirmed=True), signer=signer, ticket=ticket)
                assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationManagementAction)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationAdminAudit)) == 0
                old = await session.get(IntegrationCredential, key.credential.selector, populate_existing=True)
                assert old.rotated_at is None
                assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationCredential)) == 1
            return
        other = replace(target, action=ManagementAction.REVOKE) if kind == "rotate_revoke" else target
        other_ticket = new_management_ticket(signer, owner, other) if kind == "rotate_revoke" else ticket
        results = await asyncio.gather(run(target, ticket), run(other, other_ticket))
        async with sessions() as session:
            if kind == "rotate_revoke":
                old = await session.get(IntegrationCredential, key.credential.selector)
                assert old.status == "revoked"
                assert sum(isinstance(result, ManagementResult) for result in results) in (1, 2)
                assert all(isinstance(result, ManagementResult) or result == "invalid_credential" for result in results)
                assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationAdminAudit)) == 2
            else:
                assert sum(result == "already_submitted" for result in results) == 1
                assert sum(isinstance(result, ManagementResult) for result in results) == 1
                assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationAdminAudit)) == 1
                if kind == "create":
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Integration)) == 2
                else:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(IntegrationCredential)) == 2
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["create", "issue", "rotate", "rotate_revoke", "audit_failure"])
@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_management_transactions_on_independent_connections(kind: str, dialect: str, tmp_path: Path) -> None:
    if dialect == "sqlite":
        asyncio.run(management_race(f"sqlite+aiosqlite:///{tmp_path / 'management.db'}", None, kind))
        return
    url = os.environ.get("PGCLOCK_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("PGCLOCK_TEST_POSTGRES_URL must select a disposable synthetic database")
    schema = f"phase1b_race_{uuid4().hex}"
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(sa.schema.CreateSchema(schema))
    try:
        asyncio.run(management_race(url.replace("postgresql+psycopg:", "postgresql+asyncpg:"), schema, kind))
    finally:
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()
