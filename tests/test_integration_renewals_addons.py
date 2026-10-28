"""Renewals and packs preserve PG identity and serialize immutable additive targets."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.integration_addon_models import IntegrationAddonGrant
from app.db.integration_operation_models import IntegrationOperation
from app.db.models import ServiceAddonPack, UserService
from app.services.integration_addons import addon_target, list_service_addons, set_addon_grants
from app.services.integration_execution import execute_integration_operation
from app.services.integration_observations import PanelIdentity
from app.services.integration_operations import ExecutionRequest, submit_integration_operation
from app.services.integration_pagination import IntegrationCursors
from app.services.integration_policy import IntegrationError
from test_integration_concurrency import STAFF
from test_integration_execution import FakePanel, execution_patches
from test_integration_operations import operation_database
from test_integration_service_reads import SECRET


@pytest.mark.parametrize("kind", ["renew", "volume", "duration"])
def test_additive_mutations_reuse_one_service_and_panel_identity_with_one_dispatch(kind):
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            panel = FakePanel()
            a, b, c = execution_patches(panel, access)
            with a, b, c:
                async with sessions() as session:
                    first = await submit_integration_operation(session, access, value, idempotency_key="synthetic-first-key")
                    internal = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == first.id))
                assert await execute_integration_operation(sessions, internal, panel_url="https://panel.example.invalid")
                async with sessions() as session:
                    operation = await session.get(IntegrationOperation, internal)
                    service = await session.get(UserService, operation.service_id)
                    service_id, username, internal_service = service.public_id, service.pg_username, service.id
                    if kind == "renew":
                        next_value = replace(value, kind="renew", service_id=service_id, external_operation_ref="renew-once")
                    else:
                        pack = ServiceAddonPack(name="synthetic pack", kind=kind, amount=1, price=777, owner_reseller_id=11, is_general=True, is_active=True)
                        session.add(pack)
                        await session.commit()
                        await set_addon_grants(session, STAFF, integration, frozenset({pack.id}))
                        await session.flush()
                        grant = await session.scalar(select(IntegrationAddonGrant).where(
                            IntegrationAddonGrant.integration_id == integration, IntegrationAddonGrant.pack_id == pack.id))
                        grant.public_id = "pack_77700000000000000000000000000000"
                        await session.commit()
                        catalog = await list_service_addons(session, access, value.customer_id, service_id, IntegrationCursors(SECRET))
                        assert len(catalog.items) == 1
                        offered = catalog.items[0]
                        assert offered.id == grant.public_id
                        assert set(asdict(offered)) == {
                            "id", "name", "kind", "amount_bytes", "amount_seconds", "terms_revision", "warnings"}
                        next_value = ExecutionRequest(value.customer_id, "addon-once", offered.id, offered.terms_revision, "addon", service_id)
                    mutation = await submit_integration_operation(session, access, next_value, idempotency_key="synthetic-mutate-1")
                    mutate_id = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == mutation.id))
                    with pytest.raises(IntegrationError) as error:
                        await submit_integration_operation(session, access, replace(next_value, external_operation_ref="competing-intent"), idempotency_key="competing-key-001")
                    assert error.value.code == "service_busy"
                old_limit = panel.user["data_limit"]
                assert await execute_integration_operation(sessions, mutate_id, panel_url="https://panel.example.invalid")
                assert not await execute_integration_operation(sessions, mutate_id)
                assert len(panel.writes) == 2 and panel.user["username"] == username and panel.user["id"] == 901
                if kind != "duration":
                    assert panel.user["data_limit"] == old_limit+1024**3
                else:
                    assert panel.user["data_limit"] == old_limit
                async with sessions() as session:
                    saved = await session.get(IntegrationOperation, mutate_id)
                    assert saved.state == "succeeded" and saved.service_id == internal_service
                    repeat = await submit_integration_operation(session, access, next_value, idempotency_key="synthetic-mutate-2")
                    assert repeat.id == mutation.id
    asyncio.run(scenario())


def test_addons_preserve_unlimited_dimensions_and_explicitly_activate_pending_duration():
    identity = PanelIdentity("synthetic", "pg-race", 1)
    from app.services.integration_addons import AddonTerms
    info = {"id": 1, "username": "synthetic", "admin": {"username": "pg-race"}, "used_traffic": 10,
        "status": "active", "expire": None, "data_limit": 0}
    now = int(datetime.now(timezone.utc).timestamp())
    for terms in (AddonTerms(1, "volume", "volume", 100, None), AddonTerms(2, "duration", "duration", None, 86400)):
        with pytest.raises(IntegrationError):
            addon_target(info, identity, terms, now=now)
    info.update(status="on_hold", on_hold_expire_duration=86400, data_limit=1000)
    value = addon_target(info, identity, AddonTerms(2, "duration", "duration", None, 86400), now=now)
    assert value == {"status": "active", "data_limit": 1000, "expire": now+172800}
