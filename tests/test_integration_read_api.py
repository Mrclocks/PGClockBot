"""Real gated ASGI routes enforce backend-only auth, strict requests and replay isolation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api.integration_api import register_integration_api
from app.api.integration_transport import IntegrationPeerCapture
from test_integration_service_reads import SECRET, add_service, read_database


SETTINGS = SimpleNamespace(web_secret=SECRET, integration_trusted_proxies="", integration_api_routes_enabled=False)


@asynccontextmanager
async def api_client(sessions, evidence, *, enabled=True):
    app = FastAPI()
    async def get_db():
        async with sessions() as session:
            yield session
    with patch("app.api.integration_api.get_settings", return_value=SETTINGS), patch(
        "app.api.integration_auth.get_settings", return_value=SETTINGS):
        registered = register_integration_api(app, get_db=get_db, sessions=sessions, enabled=enabled,
                                              pg_verifier=AsyncMock(return_value=evidence))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=IntegrationPeerCapture(app)), base_url="https://synthetic.test") as client:
            yield client, app, registered


def test_routers_remain_unmounted_by_default():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access):
            async with api_client(sessions, access.evidence, enabled=None) as (client, app, registered):
                assert not registered
                assert (await client.get("/api/v1/services", params={"customer_id": first.id})).status_code == 404
                assert not any(path.startswith("/api/v1") for path in app.openapi()["paths"])
    asyncio.run(scenario())


def test_authenticated_service_reads_are_scoped_allowlisted_and_private():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access):
            async with sessions() as session:
                service = await add_service(session, integration, first)
                service_id = service.public_id
            async with api_client(sessions, access.evidence) as (client, app, registered):
                headers = {"Authorization": f"Bearer {key.bearer}"}
                response = await client.get("/api/v1/services", params={"customer_id": first.id}, headers=headers)
                assert response.status_code == 200, response.text
                assert [row["id"] for row in response.json()["items"]] == [service_id]
                assert response.headers["cache-control"] == "private, no-store"
                wrong = await client.get(f"/api/v1/services/{service_id}", params={"customer_id": second.id}, headers=headers)
                assert wrong.status_code == 404 and wrong.json()["error"]["code"] == "not_found"
                assert app.openapi()["paths"]["/api/v1/services"]["get"]["security"] == [{"IntegrationBearer": []}]
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["missing_auth", "cookie", "origin", "unknown_query", "duplicate_query", "float_limit"])
def test_read_transport_rejects_ambiguous_or_invalid_inputs(case):
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access):
            async with api_client(sessions, access.evidence) as (client, app, registered):
                headers = {"Authorization": f"Bearer {key.bearer}"}
                params = [("customer_id", first.id)]
                if case == "missing_auth":
                    headers = {}
                elif case == "cookie":
                    headers["Cookie"] = "session=synthetic"
                elif case == "origin":
                    headers["Origin"] = "https://example.invalid"
                elif case == "unknown_query":
                    params.append(("shop_id", "12"))
                elif case == "duplicate_query":
                    params.append(("customer_id", second.id))
                elif case == "float_limit":
                    params.append(("limit", "1.0"))
                response = await client.get("/api/v1/services", params=params, headers=headers)
                assert response.status_code == (401 if case == "missing_auth" else 400), response.text
                assert set(response.json()) == {"error"}
                assert "synthetic" not in response.text and key.bearer not in response.text
    asyncio.run(scenario())
