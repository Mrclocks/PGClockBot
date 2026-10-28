"""Private credentials and read-only reconciliation preserve scope and saved effects."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from app.db.integration_operation_models import IntegrationCapacityCommitment, IntegrationOperation, ServiceMutationGuard
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.models import ResellerProfile, Setting, UserService
from app.services.integration_credentials import issue_credential
from app.services.integration_execution import execute_integration_operation
from app.services.integration_operations import submit_integration_operation
from app.services.integration_policy import IntegrationError
from app.services.integration_private_access import private_service_access, render_access_qr
from app.services.integration_reconciliation import claim_reconciliation, reconcile_saved_effect
from app.services.integration_service_reads import ReadAccess
from app.services.integrations import configure_integration
from test_integration_concurrency import STAFF
from test_integration_execution import FakePanel, execution_patches
from test_integration_operations import operation_database


@pytest.mark.parametrize("mismatch", [False, True])
def test_unknown_create_is_verified_with_reads_even_when_public_api_is_disabled(mismatch):
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            async with sessions() as session:
                profile = await session.get(ResellerProfile, 11)
                profile.web_permissions += ",integration_operations_reconcile"
                await session.commit()
                view = await submit_integration_operation(session, access, value, idempotency_key="synthetic-unknown-key")
                operation_id = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == view.id))
            panel = FakePanel(failure="timeout")
            a, b, c = execution_patches(panel, access)
            with a, b, c:
                assert not await execute_integration_operation(sessions, operation_id)
            assert len(panel.writes) == 1
            if mismatch:
                panel.user["admin"] = {"username": "another-admin"}
            async with sessions() as session:
                await session.execute(update(Setting).where(Setting.key == "integration_api_enabled").values(value="0"))
                await session.commit()
                lease = await claim_reconciliation(session, STAFF, integration, view.id)
            with patch("app.services.integration_reconciliation.integration_pg_client", AsyncMock(return_value=panel)), patch(
                "app.services.integration_reconciliation.fetch_pg_evidence", AsyncMock(return_value=access.evidence)), patch(
                "app.services.secret_box._root_secret", return_value=b"synthetic-encryption-root-000000000"):
                if mismatch:
                    with pytest.raises(IntegrationError):
                        await reconcile_saved_effect(sessions, lease, panel_url="https://panel.example.invalid")
                else:
                    assert await reconcile_saved_effect(sessions, lease, panel_url="https://panel.example.invalid")
            assert len(panel.writes) == 1
            async with sessions() as session:
                operation = await session.get(IntegrationOperation, operation_id)
                assert operation.state == ("outcome_unknown" if mismatch else "succeeded")
                commitment = await session.scalar(select(IntegrationCapacityCommitment).where(IntegrationCapacityCommitment.operation_id == operation_id))
                assert commitment.active == mismatch
                assert bool(await session.get(ServiceMutationGuard, operation.service_id)) == mismatch
    asyncio.run(scenario())


def test_private_access_requires_customer_and_scope_and_rotated_url_becomes_review_only():
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            panel = FakePanel()
            a, b, c = execution_patches(panel, access)
            with a, b, c:
                async with sessions() as session:
                    view = await submit_integration_operation(session, access, value, idempotency_key="synthetic-private-key")
                    operation_id = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == view.id))
                assert await execute_integration_operation(sessions, operation_id, panel_url="https://panel.example.invalid")
            async with sessions() as session:
                operation = await session.get(IntegrationOperation, operation_id)
                service = await session.get(UserService, operation.service_id)
                internal_service, public_service = service.id, service.public_id
                with pytest.raises(IntegrationError) as error:
                    await private_service_access(session, access, value.customer_id, public_service)
                assert error.value.code == "permission_denied"
                await session.rollback()
                profile = await session.get(ResellerProfile, 11)
                profile.web_permissions += ",integration_service_access"
                await session.commit()
                scopes = {"services:read", "services:access", "operations:read", "plans:read", "services:provision"}
                await configure_integration(session, STAFF, integration, status="active", grants=frozenset(scopes), customer_entry_policy="website")
                private_key = await issue_credential(session, STAFF, integration, scopes=frozenset(scopes))
                private_access = ReadAccess(private_key.bearer, access.evidence)
                with patch("app.services.integration_private_access.integration_pg_client", AsyncMock(return_value=panel)), patch(
                    "app.services.integration_private_access.get_settings", return_value=SimpleNamespace(pg_base_url="https://panel.example.invalid", pg_subscription_path="/sub")), patch(
                    "app.services.secret_box._root_secret", return_value=b"synthetic-encryption-root-000000000"):
                    with pytest.raises(IntegrationError) as error:
                        await private_service_access(session, private_access, second.id, public_service)
                    assert error.value.code == "not_found"
                    await session.rollback()
                    result = await private_service_access(session, private_access, value.customer_id, public_service)
                    assert result.subscription_url == panel.user["subscription_url"] and result.access_version == 1
                    image = await asyncio.to_thread(render_access_qr, result.subscription_url)
                    assert image.startswith(b"\x89PNG\r\n\x1a\n")
                    panel.user["subscription_url"] = "https://panel.example.invalid/sub/changed-private-url-0123456"
                    with pytest.raises(IntegrationError) as error:
                        await private_service_access(session, private_access, value.customer_id, public_service)
                    assert error.value.code == "access_not_ready"
                assert (await session.get(IntegrationServiceSnapshot, internal_service)).state == "review"
    asyncio.run(scenario())
