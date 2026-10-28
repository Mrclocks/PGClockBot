"""Saved targets, unknown effects and private sealing with a synthetic panel only."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db.integration_models import Integration
from app.db.integration_delivery_models import IntegrationDelivery
from app.db.integration_operation_models import IntegrationCapacityCommitment, IntegrationOperation, ServiceMutationGuard
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.models import Order, Payment, UserService
from app.services.integration_execution import execute_integration_operation
from app.services.integration_operations import submit_integration_operation
from app.services.integration_panel_mutations import panel_mutation
from app.services.integration_target_verification import validated_delivery_url
from app.services.integration_policy import IntegrationError
from test_integration_operations import operation_database


class FakePanel:
    def __init__(self, *, failure: str = "") -> None:
        self.failure = failure
        self.writes: list[dict] = []
        self.user: dict = {}
        self.admin = {"username": "pg-race", "status": "active", "total_users": 0, "role": {"id": 1, "limits": {"max_users": 10},
            "permissions": {"users": {"read": True, "create": True, "update": True}, "groups": {"read": True}},
            "access": {"allowed_group_ids": [1]}}}

    def _read_cache_ident(self) -> str:
        return "synthetic-own-client"

    async def get_groups_simple(self) -> list[dict]:
        return [{"id": 1}]

    async def request(self, method: str, path: str, **kwargs) -> object:
        if method != "GET":
            return await panel_mutation(self, method, path, kwargs)
        return await self._request_unfenced(method, path, **kwargs)

    async def _request_unfenced(self, method: str, path: str, **kwargs) -> object:
        if method == "GET":
            return self.admin if path == "/api/admin" else dict(self.user)
        assert kwargs["retry_auth"] is False
        self.writes.append(dict(kwargs["json"]))
        self.user = {**self.user, **kwargs["json"], "id": 901, "used_traffic": 0, "admin": {"username": "pg-race"},
            "subscription_url": "https://panel.example.invalid/sub/synthetic-secret-0123456789"}
        if self.failure == "timeout":
            raise httpx.ReadTimeout("synthetic unknown effect")
        if self.failure == "wrong_owner":
            self.user["admin"] = {"username": "another-admin"}
        if self.failure == "missing_delivery":
            self.user.pop("subscription_url")
        return dict(self.user)


def execution_patches(panel, access):
    return (patch("app.services.integration_execution.integration_pg_client", AsyncMock(return_value=panel)),
        patch("app.services.integration_execution.fetch_pg_evidence", AsyncMock(return_value=access.evidence)),
        patch("app.services.secret_box._root_secret", return_value=b"synthetic-encryption-root-000000000"))


@pytest.mark.parametrize("failure", ["", "timeout", "wrong_owner", "missing_delivery"])
def test_create_effect_is_sealed_only_after_identity_terms_and_private_delivery_verification(failure):
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            async with sessions() as session:
                view = await submit_integration_operation(session, access, value, idempotency_key="synthetic-create-1")
                internal = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == view.id))
            panel = FakePanel(failure=failure)
            a, b, c = execution_patches(panel, access)
            with a, b, c:
                result = await execute_integration_operation(sessions, internal, panel_url="https://panel.example.invalid", subscription_path="/sub")
                assert not await execute_integration_operation(sessions, internal, panel_url="https://panel.example.invalid", subscription_path="/sub")
            assert result == (not failure) and len(panel.writes) == 1
            async with sessions() as session:
                operation = await session.get(IntegrationOperation, internal)
                service = await session.get(UserService, operation.service_id)
                snapshot = await session.get(IntegrationServiceSnapshot, service.id)
                delivery = await session.get(IntegrationDelivery, service.id)
                commitment = await session.scalar(select(IntegrationCapacityCommitment).where(IntegrationCapacityCommitment.operation_id == internal))
                guard = await session.get(ServiceMutationGuard, service.id)
                assert service.bot_user_id is None and service.subscription_url is None and service.subscription_token is None
                assert operation.state == ("succeeded" if not failure else "outcome_unknown" if failure == "timeout" else "needs_review")
                assert snapshot.state == ("ready" if not failure else "review")
                assert bool(delivery) == (not failure) and commitment.active == bool(failure) and bool(guard) == bool(failure)
                if delivery:
                    assert "synthetic-secret" not in delivery.url_enc
                assert await session.scalar(select(func.count()).select_from(Order)) == 0
                assert await session.scalar(select(func.count()).select_from(Payment)) == 0
                repeated = await submit_integration_operation(session, access, value, idempotency_key="synthetic-create-2")
                assert repeated.id == view.id and "synthetic-secret" not in str(asdict(repeated))
    asyncio.run(scenario())


def test_removed_plan_before_dispatch_fails_without_any_external_write():
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            async with sessions() as session:
                view = await submit_integration_operation(session, access, value, idempotency_key="synthetic-create-1")
                operation_id = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == view.id))
                from app.db.integration_models import IntegrationPlan
                await session.execute(IntegrationPlan.__table__.delete())
                await session.commit()
            panel = FakePanel()
            a, b, c = execution_patches(panel, access)
            with a, b, c:
                assert not await execute_integration_operation(sessions, operation_id)
            assert panel.writes == []
            async with sessions() as session:
                assert (await session.get(IntegrationOperation, operation_id)).state == "failed"
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["http://panel.example.invalid/sub/abcdefghijklmnop", "https://evil.invalid/sub/abcdefghijklmnop",
    "https://panel.example.invalid/sub/abcdefghijklmnop?x=1", "https://a@panel.example.invalid/sub/abcdefghijklmnop",
    "https://panel.example.invalid/sub/%61bcdefghijklmnop", "https://panel.example.invalid/sub/abcdefghijklmnop\n"])
def test_delivery_rejects_unconfigured_or_ambiguous_bearer_urls(url):
    with pytest.raises(IntegrationError):
        validated_delivery_url({"subscription_url": url}, panel_url="https://panel.example.invalid", subscription_path="/sub")


def test_disabling_an_integration_after_acceptance_fails_unstarted_work_without_writing():
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            async with sessions() as session:
                view = await submit_integration_operation(session, access, value, idempotency_key="synthetic-disabled-key")
                operation_id = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == view.id))
                await session.execute(update(Integration).where(Integration.id == integration).values(status="disabled"))
                await session.commit()
            panel = FakePanel()
            a, b, c = execution_patches(panel, access)
            with a, b, c:
                assert not await execute_integration_operation(sessions, operation_id)
            assert panel.writes == []
            async with sessions() as session:
                saved = await session.get(IntegrationOperation, operation_id)
                assert saved.state == "failed" and saved.failure_code == "integration_disabled"
    asyncio.run(scenario())
