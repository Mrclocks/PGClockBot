"""Neutral identities, current authority, explicit entry policy and atomic registration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable
from typing import Any
from dataclasses import asdict
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.db.customer_models import Customer, IntegrationCustomer
from app.db.integration_models import Integration, IntegrationCredential
from app.db.models import BotUser, OrgPrincipal, OrgPrincipalWebIdentity, ResellerProfile, Setting
from app.services.integration_customers import CustomerInput, create_or_get_integration_customer as register, read_integration_customer as read
from app.services.integration_credentials import CredentialIssuance, issue_credential, revoke_credential
from app.services.integration_policy import IntegrationError
from app.services.integrations import configure_integration, create_integration
from test_integration_concurrency import STAFF, seed

SCOPES = frozenset({"customers:read", "customers:write"})


@asynccontextmanager
async def database(
    url: str = "sqlite+aiosqlite:///:memory:", schema: str | None = None,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str, CredentialIssuance]]:
    options = {"connect_args": {"server_settings": {"search_path": schema}}} if schema else {}
    engine = create_async_engine(url, **options)
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def configure(connection: Any, record: Any) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            integration_id = await seed(session)
            await session.execute(update(ResellerProfile).values(web_permissions=
                "integrations_manage,integration_customers_read,integration_customers_write"))
            await session.commit()
            await configure_integration(session, STAFF, integration_id, status="active", grants=SCOPES,
                                        customer_entry_policy="website")
            key = await issue_credential(session, STAFF, integration_id, scopes=SCOPES)
        yield sessions, integration_id, key
    finally:
        await engine.dispose()


async def error(awaitable: Awaitable[object], code: str) -> None:
    with pytest.raises(IntegrationError) as caught:
        await awaitable
    assert caught.value.code == code


def test_replay_is_stable_and_conflicting_name_never_updates_identity() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            value = CustomerInput("Customer:1", "<script>{{ hostile }}</script>")
            first = await register(session, key.bearer, value)
            repeat = await register(session, key.bearer, value)
            assert first.created and not repeat.created and first.customer == repeat.customer
            assert first.customer.id.startswith("cus_") and not first.customer.telegram_linked
            assert set(asdict(first.customer)) == {"id", "external_customer_ref", "display_name", "state", "telegram_linked", "created_at"}
            await error(register(session, key.bearer, CustomerInput("Customer:1", "different")), "customer_reference_conflict")
            assert await read(session, key.bearer, first.customer.id) == first.customer
            assert await session.scalar(select(func.count()).select_from(Customer)) == 1
            assert await session.scalar(select(func.count()).select_from(BotUser)) == 1
            assert (await register(session, key.bearer, CustomerInput("customer:1"))).created
    asyncio.run(scenario())


@pytest.mark.parametrize("reference", ["", "x"*129, " space", "space ", "a/b", "سلام", "a\n", True, 10, None])
def test_invalid_references_are_rejected_without_normalization(reference: object) -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            await error(register(session, key.bearer, CustomerInput(reference)), "invalid_request")
            assert await session.scalar(select(func.count()).select_from(Customer)) == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["x"*129, "a\x00b", "a\nb", "a\x7fb", "\ud800", True, 1, None])
def test_display_name_bounds_and_controls(name: object) -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            await error(register(session, key.bearer, CustomerInput("ref", name)), "invalid_request")
    asyncio.run(scenario())


def test_reference_and_unicode_name_at_exact_bounds() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            value = CustomerInput("A"*128, "😀"*128)
            result = await register(session, key.bearer, value)
            assert result.customer.display_name == value.display_name
    asyncio.run(scenario())


@pytest.mark.parametrize("change,code", [
    ("policy", "website_entry_not_enabled"), ("grant", "permission_denied"),
    ("credential_scope", "permission_denied"), ("disabled", "integration_disabled"),
    ("revoked", "invalid_credential"), ("permission", "permission_denied"),
    ("principal", "principal_inactive"), ("blocked_owner", "principal_inactive"), ("api", "api_disabled"),
])
def test_registration_uses_current_authority_and_independent_entry_approval(change: str, code: str) -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            from app.services.integration_authority import resolve_integration_authority
            await resolve_integration_authority(session, key.bearer, required_scopes=SCOPES)
            statements = {
                "policy": update(Integration).values(customer_entry_policy="denied"),
                "grant": update(Integration).values(grants="customers:read"),
                "credential_scope": update(IntegrationCredential).values(scopes="customers:read"),
                "disabled": update(Integration).values(status="disabled"),
                "revoked": update(IntegrationCredential).values(status="revoked"),
                "permission": update(ResellerProfile).values(web_permissions="integrations_manage,integration_customers_read"),
                "principal": update(OrgPrincipal).where(OrgPrincipal.id == 11).values(status="disabled"),
                "blocked_owner": update(BotUser).values(is_blocked=True),
                "api": update(Setting).where(Setting.key == "integration_api_enabled").values(value="0"),
            }
            async with sessions() as other:
                await other.execute(statements[change])
                await other.commit()
            await error(register(session, key.bearer, CustomerInput("ref")), code)
            assert await session.scalar(select(func.count()).select_from(Customer)) == 0
    asyncio.run(scenario())


def test_integrations_and_shops_never_merge_matching_references_or_adopt_legacy_customers() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            first = (await register(session, key.bearer, CustomerInput("same"))).customer
            other = await create_integration(session, STAFF, grants=SCOPES)
            await configure_integration(session, STAFF, other.id, status="active", grants=SCOPES, customer_entry_policy="website")
            other_key = await issue_credential(session, STAFF, other.id, scopes=SCOPES)
            second = (await register(session, other_key.bearer, CustomerInput("same"))).customer
            assert first.id != second.id
            await error(read(session, key.bearer, second.id), "not_found")
            await session.rollback()
            await error(read(session, other_key.bearer, first.id), "not_found")
            await session.rollback()
            # An independent shop cannot resolve or mutate the first shop's identity.
            from datetime import datetime, timezone
            session.add(BotUser(id=12, telegram_id=10012, referral_code="shop-twelve"))
            await session.flush()
            session.add(ResellerProfile(id=12, user_id=12, pg_admin_username="pg-twelve", is_active=True,
                setup_completed_at=datetime.now(timezone.utc), web_username="twelve", web_password_hash="synthetic-hash",
                web_permissions="integrations_manage,integration_customers_read,integration_customers_write"))
            await session.flush()
            session.add(OrgPrincipal(id=12, parent_id=1, depth=1, status="active", reseller_profile_id=12,
                bot_user_id=12, pg_username="pg-twelve", pg_api_key_enc="synthetic-key"))
            await session.flush()
            session.add(OrgPrincipalWebIdentity(id=12, principal_id=12, is_active=True,
                web_username="twelve", web_password_hash="synthetic-hash"))
            await session.commit()
            staff = {"role": "principal", "web_identity_id": 12}
            third = await create_integration(session, staff, grants=SCOPES)
            await configure_integration(session, staff, third.id, status="active", grants=SCOPES, customer_entry_policy="website")
            third_key = await issue_credential(session, staff, third.id, scopes=SCOPES)
            third_customer = (await register(session, third_key.bearer, CustomerInput("same"))).customer
            assert third_customer.id not in {first.id, second.id}
            await error(read(session, third_key.bearer, first.id), "not_found")
            await session.rollback()
            await error(read(session, key.bearer, third_customer.id), "not_found")
            await session.rollback()
            from app.services.customer_identity import ensure_legacy_customer
            legacy = await ensure_legacy_customer(session, 11)
            await session.commit()
            public = await session.scalar(select(Customer.public_id).where(Customer.id == legacy))
            await error(read(session, key.bearer, public), "not_found")
            assert await session.scalar(select(func.count()).select_from(IntegrationCustomer)) == 3
    asyncio.run(scenario())


def test_blocked_customer_cannot_be_read_or_reactivated_on_replay() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            first = await register(session, key.bearer, CustomerInput("ref"))
            await session.execute(update(Customer).values(state="blocked"))
            await session.commit()
            await error(read(session, key.bearer, first.customer.id), "customer_blocked")
            await session.rollback()
            await error(register(session, key.bearer, CustomerInput("ref")), "customer_blocked")
            assert await session.scalar(select(Customer.state)) == "blocked"
    asyncio.run(scenario())


def test_registration_failure_or_caller_rollback_leaves_no_orphan_customer() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            original = session.flush
            calls = 0
            async def fail_mapping(*args: Any, **kwargs: Any) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("synthetic mapping failure")
                return await original(*args, **kwargs)
            with patch.object(session, "flush", side_effect=fail_mapping), pytest.raises(RuntimeError):
                await register(session, key.bearer, CustomerInput("ref"))
            assert await session.scalar(select(func.count()).select_from(Customer)) == 0
            await register(session, key.bearer, CustomerInput("ref"), commit=False)
            await session.rollback()
            assert await session.scalar(select(func.count()).select_from(Customer)) == 0
            assert await session.scalar(select(func.count()).select_from(IntegrationCustomer)) == 0
    asyncio.run(scenario())


def test_revoked_credential_cannot_read_and_missing_customer_is_safe() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            first = await register(session, key.bearer, CustomerInput("ref"))
            await error(read(session, key.bearer, "cus_invalid"), "not_found")
            await session.rollback()
            await revoke_credential(session, STAFF, integration, key.credential.selector)
            await error(read(session, key.bearer, first.customer.id), "invalid_credential")
    asyncio.run(scenario())


def test_mapping_reference_customer_and_adapter_identity_are_immutable() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            result = await register(session, key.bearer, CustomerInput("ref"))
            mapping = (await session.scalars(select(IntegrationCustomer))).one()
            mapping.external_customer_ref = "replacement"
            with pytest.raises(ValueError, match="immutable"):
                await session.flush()
            await session.rollback()
            customer = await session.scalar(select(Customer).where(Customer.public_id == result.customer.id))
            customer.legacy_bot_user_id = 11
            with pytest.raises(ValueError, match="immutable"):
                await session.flush()
            await session.rollback()
            assert (await read(session, key.bearer, result.customer.id)).external_customer_ref == "ref"
    asyncio.run(scenario())
