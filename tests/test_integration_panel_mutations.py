"""Native PG paths cannot bypass API guards or repeat uncertain effects."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.db.integration_operation_models import IntegrationNativeMutation, ServiceMutationGuard
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.services.integration_controls import control_page
from test_integration_concurrency import STAFF
from app.services.integration_panel_mutations import panel_mutation
from app.services.integration_policy import IntegrationError
from test_integration_service_reads import add_service, read_database


class NativePanel:
    def __init__(self, sessions, *, failure=False):
        self._integration_sessions = sessions
        self.failure = failure
        self.writes = []
        self.status = "active"

    async def _request_unfenced(self, method, path, **kwargs):
        if method == "GET":
            return {"id": 701, "username": "synthetic-web-701", "status": self.status, "admin": {"username": "pg-race"}}
        assert kwargs.get("retry_auth") is False
        self.writes.append((method, path, kwargs))
        self.status = "disabled"
        if self.failure:
            raise httpx.ReadTimeout("synthetic unknown native status")
        return {"status": self.status}


@pytest.mark.parametrize("path,method,payload", [
    ("/api/user/by-id/701", "DELETE", {}), ("/api/user/by-id/701/reset", "POST", {}),
    ("/api/user/by-id/701/revoke_sub", "POST", {}), ("/api/user/from_template/by-id/701", "PUT", {"data_limit": 3000}),
    ("/api/user/by-id/701", "PUT", {"status": "active", "data_limit": 3000}),
    ("/api/user/by-username/synthetic-web-701/set_owner", "PUT", {}),
])
def test_native_quota_reset_revoke_transfer_and_delete_are_blocked_for_api_resources(path, method, payload):
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access), sessions() as session:
            await add_service(session, integration, first)
            panel = NativePanel(sessions)
            with pytest.raises(IntegrationError):
                await panel_mutation(panel, method, path, {"json": payload})
            assert panel.writes == []
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_billing_disabled_path_is_guarded_and_uncertainty_remains_fenced(failure):
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access), sessions() as session:
            service = await add_service(session, integration, first)
            panel = NativePanel(sessions, failure=failure)
            if failure:
                with pytest.raises(httpx.ReadTimeout):
                    await panel_mutation(panel, "PUT", "/api/user/by-id/701/disabled", {"json": {"disabled": True}})
                with pytest.raises(IntegrationError):
                    await panel_mutation(panel, "PUT", "/api/user/by-id/701/disabled", {"json": {"disabled": True}})
            else:
                await panel_mutation(panel, "PUT", "/api/user/by-id/701/disabled", {"json": {"disabled": True}})
            assert len(panel.writes) == 1
            await session.rollback()
            evidence = await session.scalar(select(IntegrationNativeMutation))
            snapshot = await session.get(IntegrationServiceSnapshot, service.id)
            assert evidence.state == ("unknown" if failure else "completed")
            assert snapshot.state == ("review" if failure else "ready")
            assert bool(await session.get(ServiceMutationGuard, service.id)) == failure
            page = await control_page(session, STAFF, integration)
            assert [row.service_id for row in page.native] == ([service.public_id] if failure else [])
    asyncio.run(scenario())
