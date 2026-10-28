"""Durable intents survive key rotation, request expiry, competing claims and unknown outcomes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db.integration_models import IntegrationPlan
from app.db.integration_operation_models import IntegrationCapacityCommitment, IntegrationOperation
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.models import Plan, ResellerProfile, Setting, UserService
from app.db.integration_models import Integration
from app.services.integration_catalog import CatalogSelection, list_integration_plans
from app.services.integration_credentials import issue_credential, rotate_credential
from app.services.integration_operation_claims import expire_queued_operation, claim_operation, heartbeat_operation, recover_expired_operation, transition_operation
from app.services.integration_operation_targets import PanelTarget, mark_dispatch_intent, mark_dispatch_sent, save_operation_target
from app.services.integration_operations import ExecutionRequest, submit_integration_operation
from app.services.integration_pagination import IntegrationCursors
from app.services.integration_policy import IntegrationError
from app.services.integration_service_reads import ReadAccess
from app.services.integrations import configure_integration
from app.services.volume_allocation import sum_active_holds
from app.services.integration_capacity import durable_user_slots
from app.services import integration_runtime, users
from app.services.pg_quota import _reserved_user_slots
from test_integration_concurrency import STAFF
from test_integration_service_reads import SCOPES, SECRET, read_database


@asynccontextmanager
async def operation_database(url="sqlite+aiosqlite:///:memory:", schema=None):
    async with read_database(url, schema) as (sessions, integration, key, first, second, plan, access):
        async with sessions() as session:
            profile = await session.get(ResellerProfile, 11)
            profile.web_permissions += ",integration_services_provision,integration_services_renew,integration_services_addons"
            await session.commit()
            scopes = SCOPES | {"services:provision", "services:renew", "services:addons"}
            await configure_integration(session, STAFF, integration, status="active", grants=scopes, customer_entry_policy="website")
            key = await issue_credential(session, STAFF, integration, scopes=scopes)
            access = ReadAccess(key.bearer, access.evidence)
            client = AsyncMock()
            client.get_groups_simple.return_value = [{"id": 1}]
            client.request.return_value = {"username": "pg-race", "role": {"id": 1, "permissions": {
                "users": {"read": True, "create": True, "update": True}, "groups": {"read": True}},
                "access": {"allowed_group_ids": [1]}}}
            with patch("app.services.integration_catalog.integration_pg_client", AsyncMock(return_value=client)):
                catalog = await list_integration_plans(session, access, CatalogSelection(), IntegrationCursors(SECRET))
            offered = catalog.items[0]
            value = ExecutionRequest(first.id, "intent-1", offered.id, offered.technical_terms.terms_revision)
        with patch.object(get_settings(), "integration_execution_enabled", True), patch(
            "app.services.integration_runtime._api_enabled", True,
        ), patch("app.services.integration_runtime._has_execution_history", False):
            yield sessions, integration, key, access, value, second


def test_external_intent_is_permanent_across_new_keys_rotation_and_catalog_removal():
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second), sessions() as session:
            first = await submit_integration_operation(session, access, value, idempotency_key="submit-first-key-1")
            replacement = await rotate_credential(session, STAFF, integration, key.credential.selector)
            access = ReadAccess(replacement.bearer, access.evidence)
            await session.execute(IntegrationPlan.__table__.delete())
            await session.commit()
            repeat = await submit_integration_operation(session, access, value, idempotency_key="submit-second-key")
            assert repeat.id == first.id and repeat.state == "queued"
            for changed, expected in ((replace(value, external_operation_ref="different"), "idempotency_conflict"),
                                      (replace(value, customer_id=second.id), "external_reference_conflict")):
                with pytest.raises(IntegrationError) as error:
                    await submit_integration_operation(session, access, changed,
                        idempotency_key="submit-first-key-1" if expected == "idempotency_conflict" else "submit-third-key-1")
                assert error.value.code == expected
            assert await session.scalar(select(func.count()).select_from(IntegrationOperation)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("dispatched", [False, True])
def test_expired_lease_requeues_only_before_intent_and_never_releases_unknown_capacity(dispatched):
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second), sessions() as session:
            view = await submit_integration_operation(session, access, value, idempotency_key="submit-first-key-1")
            operation_id = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == view.id))
            now = datetime.now(timezone.utc)
            lease = await claim_operation(session, operation_id, now=now)
            session.add(IntegrationCapacityCommitment(operation_id=operation_id, shop_id=11, principal_id=11,
                pg_username="pg-race", bytes=1000, user_slots=1, created_at=now-timedelta(days=100)))
            target = PanelTarget("POST", "/api/user", {"username": "saved-target"}, "saved-target", "pg-race", None, "marker")
            await save_operation_target(session, lease, target)
            await session.commit()
            if dispatched:
                await mark_dispatch_intent(session, lease)
                await mark_dispatch_sent(session, lease)
                with pytest.raises(IntegrationError):
                    await mark_dispatch_sent(session, lease)
                await session.rollback()
            state = await recover_expired_operation(session, operation_id, now=now+timedelta(seconds=61))
            assert state == ("outcome_unknown" if dispatched else "queued")
            assert not await heartbeat_operation(session, lease)
            assert await sum_active_holds(session, 11) == 1000
            assert await durable_user_slots(session, shop_id=11) == 1
            assert await durable_user_slots(session, shop_id=12, pg_username="pg-race") == 1
            assert await durable_user_slots(session, shop_id=12, pg_username="different-admin") == 0
            if dispatched:
                assert await claim_operation(session, operation_id) is None
                with pytest.raises(IntegrationError):
                    await transition_operation(session, lease, "failed", code="internal_error")
            assert await session.scalar(select(IntegrationOperation.target_json).where(IntegrationOperation.id == operation_id)) == target.serialized()
    asyncio.run(scenario())


def test_disabling_execution_and_restarting_keeps_uncertain_capacity_reserved() -> None:
    async def scenario() -> None:
        async with operation_database() as (sessions, integration, key, access, value, second), sessions() as session:
            view = await submit_integration_operation(session, access, value, idempotency_key="saved-capacity-marker")
            operation_id = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == view.id))
            lease = await claim_operation(session, operation_id)
            session.add(IntegrationCapacityCommitment(operation_id=operation_id, shop_id=11, principal_id=11,
                pg_username="pg-race", bytes=1000, user_slots=1))
            target = PanelTarget("POST", "/api/user", {"username": "retained-target"}, "retained-target", "pg-race", None, "marker")
            await save_operation_target(session, lease, target)
            await session.commit()
            assert await session.scalar(select(Setting.value).where(Setting.key == integration_runtime.EXECUTION_HISTORY_KEY)) == "1"
            with patch.object(get_settings(), "integration_execution_enabled", False), patch.object(
                integration_runtime, "_api_enabled", False,
            ), patch.object(integration_runtime, "_has_execution_history", False), patch.object(users, "_defaults_ready", False):
                await users.ensure_default_settings(session)
                assert not integration_runtime.integration_execution_active()
                assert integration_runtime.integration_capacity_retained()
                assert await sum_active_holds(session, 11) == 1000
                assert await _reserved_user_slots(session, {
                    "role": "reseller", "bot_user_id": 11, "pg_admin_username": "pg-race",
                }) == 1
                integration_runtime.observe_execution_history(set())
                assert await sum_active_holds(session, 11) == 1000
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["policy", "terms"])
def test_new_intents_reject_withdrawn_website_policy_or_changed_local_terms(change):
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second), sessions() as session:
            if change == "policy":
                await session.execute(update(Integration).where(Integration.id == integration).values(customer_entry_policy="denied"))
            else:
                await session.execute(update(Plan).values(duration_days=31))
            await session.commit()
            with pytest.raises(IntegrationError) as error:
                await submit_integration_operation(session, access, value, idempotency_key="synthetic-changed-policy")
            assert error.value.code == ("website_entry_not_enabled" if change == "policy" else "terms_changed")
            assert await session.scalar(select(func.count()).select_from(IntegrationOperation)) == 0
    asyncio.run(scenario())


def test_queue_deadline_cancels_a_saved_but_undispatched_service_and_releases_only_its_commitment():
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second), sessions() as session:
            view = await submit_integration_operation(session, access, value, idempotency_key="synthetic-expiry-key")
            operation = await session.scalar(select(IntegrationOperation).where(IntegrationOperation.public_id == view.id))
            operation_id = operation.id
            service = UserService(integration_id=integration, customer_id=operation.customer_id, origin_operation_id=operation.public_id,
                owner_principal_id=11, shop_id=11, pg_username="saved-undispatched-user")
            session.add(service)
            await session.flush()
            operation.service_id = service.id
            snapshot = IntegrationServiceSnapshot(service_id=service.id)
            session.add(snapshot)
            session.add(IntegrationCapacityCommitment(operation_id=operation_id, principal_id=11, shop_id=11, bytes=123, user_slots=1))
            await session.commit()
            deadline = datetime.now(timezone.utc)+timedelta(hours=25)
            assert await expire_queued_operation(session, operation_id, now=deadline)
            assert not await expire_queued_operation(session, operation_id, now=deadline)
            await session.refresh(snapshot)
            assert snapshot.state == "cancelled"
            assert await sum_active_holds(session, 11) == 0
    asyncio.run(scenario())
