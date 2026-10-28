"""Scoped local snapshots, safe projections, keyset traversal and bounded detail refresh."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.db.customer_models import Customer
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.models import Plan, ResellerProfile, UserService
from app.services.integration_authority import IntegrationPGEvidence
from app.services.integration_credentials import issue_credential
from app.services.integration_customers import CustomerInput, create_or_get_integration_customer
from app.services.integration_pagination import IntegrationCursors
from app.services.integration_policy import IntegrationError
from app.services.integration_service_reads import ReadAccess, ServiceSelection, list_integration_services, read_integration_service
from app.services.integrations import configure_integration, set_integration_plans
from test_integration_concurrency import STAFF
from test_integration_customers import database

SCOPES = frozenset({"customers:read", "customers:write", "services:read", "plans:read", "telegram:link", "operations:read"})
SECRET = "synthetic-read-api-key-0000000001"


@asynccontextmanager
async def read_database(url="sqlite+aiosqlite:///:memory:", schema=None):
    async with database(url, schema) as (sessions, integration, key):
        async with sessions() as session:
            await session.execute(update(ResellerProfile).values(web_permissions=
                "integrations_manage,integration_customers_read,integration_customers_write,plans,integration_plans_read,"
                "integration_services_read,integration_telegram_link,integration_operations_read"))
            await session.commit()
            await configure_integration(session, STAFF, integration, status="active", grants=SCOPES, customer_entry_policy="website")
            key = await issue_credential(session, STAFF, integration, scopes=SCOPES)
            first = (await create_or_get_integration_customer(session, key.bearer, CustomerInput("web-1"))).customer
            second = (await create_or_get_integration_customer(session, key.bearer, CustomerInput("web-2"))).customer
            plan = Plan(name="synthetic", price=99, duration_days=30, data_limit_gb=1, pg_group_ids="1", owner_reseller_id=11)
            session.add(plan)
            await session.commit()
            await set_integration_plans(session, STAFF, integration, plan_ids=frozenset({plan.id}))
            plan_id = plan.id
        evidence = IntegrationPGEvidence(11, "pg-race", datetime.now(timezone.utc), True,
                                        frozenset({"read", "create", "update"}), frozenset({"templates", "groups"}))
        yield sessions, integration, key, first, second, plan_id, ReadAccess(key.bearer, evidence)


async def add_service(session, integration, customer, *, panel_id=701, plan_id=None, **snapshot_values):
    canonical_id = await session.scalar(select(Customer.id).where(Customer.public_id == customer.id))
    service = UserService(customer_id=canonical_id, integration_id=integration, origin_operation_id=f"op_{uuid4()}",
        owner_principal_id=11, shop_id=11, pg_user_id=panel_id, pg_username=f"synthetic-web-{panel_id}", plan_id=plan_id)
    session.add(service)
    await session.flush()
    now = datetime.now(timezone.utc)
    values = {"state": "ready", "panel_status": "active", "limit_bytes": 1000, "used_bytes": 50,
              "volume_unlimited": False, "time_unlimited": False, "expires_at": now+timedelta(days=1), "synced_at": now}
    values.update(snapshot_values)
    session.add(IntegrationServiceSnapshot(service_id=service.id, **values))
    await session.commit()
    return service


def test_lists_are_customer_scoped_and_make_no_panel_calls():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access), sessions() as session:
            service = await add_service(session, integration, first)
            await add_service(session, integration, second, panel_id=702)
            with patch("app.services.integration_service_reads.integration_pg_client", AsyncMock()) as panel:
                page = await list_integration_services(session, access, ServiceSelection(first.id), IntegrationCursors(SECRET))
                panel.assert_not_called()
            assert [row.id for row in page.items] == [service.public_id]
            assert set(asdict(page.items[0])) == {"id", "customer_id", "plan_id", "state", "panel_status", "quota", "freshness", "resource_version", "created_at"}
            assert page.items[0].quota.remaining_bytes == 950
            with pytest.raises(IntegrationError) as error:
                await read_integration_service(session, access, second.id, service.public_id, refresh=False)
            assert error.value.code == "not_found"
    asyncio.run(scenario())


def test_keyset_high_water_and_customer_binding_survive_new_insert():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access), sessions() as session:
            for panel_id in (701, 702, 703):
                await add_service(session, integration, first, panel_id=panel_id)
            codec = IntegrationCursors(SECRET)
            page = await list_integration_services(session, access, ServiceSelection(first.id, limit=2), codec)
            new = await add_service(session, integration, first, panel_id=704)
            rest = await list_integration_services(session, access, ServiceSelection(first.id, page.next_cursor, 2), codec)
            assert len(rest.items) == 1 and not rest.has_more
            assert new.public_id not in {item.id for item in (*page.items, *rest.items)}
            with pytest.raises(IntegrationError) as error:
                await list_integration_services(session, access, ServiceSelection(second.id, page.next_cursor, 2), codec)
            assert error.value.code == "invalid_cursor"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "mismatch", "valid"])
def test_detail_refresh_never_holds_a_transaction_and_preserves_safe_fallback(failure):
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access), sessions() as session:
            old_sync = datetime.now(timezone.utc)-timedelta(minutes=10)
            service = await add_service(session, integration, first, synced_at=old_sync)
            public_id = service.public_id
            async def fetch(panel_id):
                assert not session.in_transaction()
                if failure == "timeout":
                    raise httpx.ReadTimeout("synthetic outage")
                return {"id": panel_id, "username": "wrong" if failure == "mismatch" else "synthetic-web-701",
                        "admin": {"username": "pg-race"}, "status": "active", "data_limit": 1000, "used_traffic": 100,
                        "expire": (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                        "subscription_url": "https://example.invalid/private-secret", "links": ["private-config"]}
            client = AsyncMock()
            client.get_user_by_id.side_effect = fetch
            with patch("app.services.integration_service_reads.integration_pg_client", AsyncMock(return_value=client)):
                view = await read_integration_service(session, access, first.id, public_id)
            assert "private-secret" not in str(asdict(view)) and "private-config" not in str(asdict(view))
            if failure == "valid":
                assert view.quota.used_bytes == 100 and view.freshness.source == "panel" and view.freshness.freshness == "fresh"
            else:
                assert view.quota.used_bytes == 50 and view.freshness.freshness == "stale"
                assert view.state == ("review" if failure == "mismatch" else "ready")
                assert view.freshness.panel_available == (failure == "mismatch")
    asyncio.run(scenario())


def test_unknown_numeric_fields_are_not_zero_or_unlimited():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access), sessions() as session:
            await add_service(session, integration, first, limit_bytes=None, used_bytes=None, expires_at=None,
                              synced_at=None, volume_unlimited=None, time_unlimited=None)
            page = await list_integration_services(session, access, ServiceSelection(first.id), IntegrationCursors(SECRET))
            view = page.items[0]
            assert view.freshness.freshness == "unknown"
            assert all(value is None for value in asdict(view.quota).values())
    asyncio.run(scenario())


def test_customer_blocked_during_refresh_cannot_update_or_read_the_service():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access), sessions() as session:
            service = await add_service(session, integration, first)
            internal_id, public_id, customer_id = service.id, service.public_id, service.customer_id
            async def fetch(panel_id):
                async with sessions() as other:
                    await other.execute(update(Customer).where(Customer.id == customer_id).values(state="blocked"))
                    await other.commit()
                return {"id": panel_id, "username": "synthetic-web-701", "admin": "pg-race", "status": "active",
                        "data_limit": 1000, "used_traffic": 999, "expire": None}
            client = AsyncMock()
            client.get_user_by_id.side_effect = fetch
            with patch("app.services.integration_service_reads.integration_pg_client", AsyncMock(return_value=client)):
                with pytest.raises(IntegrationError):
                    await read_integration_service(session, access, first.id, public_id)
            assert (await session.get(IntegrationServiceSnapshot, internal_id)).used_bytes == 50
    asyncio.run(scenario())
