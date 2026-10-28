"""Real backend routes expose only accepted intents and strict generic schemas."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sqlalchemy import select

from app.cli.integration_openapi import integration_openapi
from app.db.integration_operation_models import IntegrationOperation
from app.db.models import ResellerProfile
from app.services.integration_credentials import issue_credential
from app.services.integration_execution import execute_integration_operation
from app.services.integration_service_reads import ReadAccess
from app.services.integrations import configure_integration
from docs.examples.integration_backend import IntegrationBackend, verify_webhook
from app.services.integration_webhooks import signing_headers
from test_integration_concurrency import STAFF
from test_integration_execution import FakePanel, execution_patches
from test_integration_operations import operation_database
from test_integration_read_api import api_client

def test_submissions_return_one_durable_resource_with_location_and_safe_conflict():
    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            async with api_client(sessions, access.evidence) as (client, app, registered):
                headers = {"Authorization": f"Bearer {key.bearer}", "Idempotency-Key": "synthetic-accept-http"}
                body = {"customer_id": value.customer_id, "plan_id": value.catalog_id,
                    "external_operation_ref": value.external_operation_ref, "terms_revision": value.terms_revision}
                with patch("app.api.integration_operation_routes.get_settings", return_value=SimpleNamespace(integration_execution_enabled=True)):
                    first = await client.post("/api/v1/provisioning-requests", json=body, headers=headers)
                    assert first.status_code == 202 and "no-store" in first.headers["cache-control"]
                    assert first.headers["location"].startswith("/api/v1/operations/") and first.headers["retry-after"] == "5"
                    repeat = await client.post("/api/v1/provisioning-requests", json=body,
                        headers={**headers, "Idempotency-Key": "synthetic-repeat-http"})
                    assert repeat.json()["id"] == first.json()["id"]
                    conflict = await client.post("/api/v1/provisioning-requests", json={**body, "external_operation_ref": "changed"}, headers=headers)
                    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
                    wrong = await client.get(first.headers["location"].split("?")[0], params={"customer_id": second.id}, headers={"Authorization": headers["Authorization"]})
                    assert wrong.status_code == 404
                    polled = await client.get(first.headers["location"], headers={"Authorization": headers["Authorization"]})
                    assert polled.status_code == 200 and polled.json()["id"] == first.json()["id"]
                with patch("app.api.integration_operation_routes.get_settings", return_value=SimpleNamespace(integration_execution_enabled=False)):
                    stopped = await client.post("/api/v1/provisioning-requests", json=body, headers=headers)
                    assert stopped.status_code == 503
    asyncio.run(scenario())


def test_openapi_exports_actual_private_binary_and_error_contract_without_requests():
    document = integration_openapi()
    assert document["paths"]["/api/v1/provisioning-requests"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("OperationView")
    qr = document["paths"]["/api/v1/services/{service_id}/qr"]["get"]
    assert qr["responses"]["200"]["content"]["image/png"]["schema"]["format"] == "binary"
    assert qr["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("IntegrationError")
    assert qr["security"] == [{"IntegrationBearer": []}]


def test_generic_backend_client_reaches_verified_service_and_private_access_with_fake_panel():

    async def scenario():
        async with operation_database() as (sessions, integration, key, access, value, second):
            async with sessions() as session:
                profile = await session.get(ResellerProfile, 11)
                profile.web_permissions += ",integration_service_access"
                await session.commit()
                scopes = frozenset({"services:read", "services:access", "operations:read", "services:provision", "plans:read"})
                await configure_integration(session, STAFF, integration, status="active", grants=scopes, customer_entry_policy="website")
                key = await issue_credential(session, STAFF, integration, scopes=scopes)
                access = ReadAccess(key.bearer, access.evidence)
            panel = FakePanel()
            async with api_client(sessions, access.evidence) as (client, app, registered):
                backend = IntegrationBackend(client, key.bearer)
                with patch("app.api.integration_operation_routes.get_settings", return_value=SimpleNamespace(integration_execution_enabled=True)):
                    accepted = await backend.submit_provision(customer_id=value.customer_id, plan_id=value.catalog_id,
                        terms_revision=value.terms_revision, external_operation_ref="generic-backend-intent", idempotency_key="generic-backend-key")
                async with sessions() as session:
                    internal = await session.scalar(select(IntegrationOperation.id).where(IntegrationOperation.public_id == accepted["id"]))
                a, b, c = execution_patches(panel, access)
                with a, b, c:
                    assert await execute_integration_operation(sessions, internal, panel_url="https://panel.example.invalid")
                polled = await backend.poll_operation(customer_id=value.customer_id, operation_id=accepted["id"])
                assert polled["state"] == "succeeded" and "subscription_url" not in polled
                with patch("app.services.integration_private_access.integration_pg_client", AsyncMock(return_value=panel)), patch(
                    "app.services.integration_private_access.get_settings", return_value=SimpleNamespace(pg_base_url="https://panel.example.invalid", pg_subscription_path="/sub")), patch(
                    "app.services.secret_box._root_secret", return_value=b"synthetic-encryption-root-000000000"):
                    response = await client.get(f"/api/v1/services/{polled['service_id']}/access", params={"customer_id": value.customer_id},
                        headers={"Authorization": f"Bearer {key.bearer}"})
                    assert response.status_code == 200 and response.json()["subscription_url"] == panel.user["subscription_url"]
                    qr = await client.get(f"/api/v1/services/{polled['service_id']}/qr", params={"customer_id": value.customer_id},
                        headers={"Authorization": f"Bearer {key.bearer}"})
                    assert qr.status_code == 200 and qr.headers["x-access-version"] == str(response.json()["access_version"])
                    assert qr.content.startswith(b"\x89PNG") and "no-store" in qr.headers["cache-control"]
    asyncio.run(scenario())


def test_generic_webhook_verifier_accepts_http_header_case_and_rejects_tampering():
    event_id, secret = "evt_"+"0"*32, "synthetic-webhook-key"
    body = json.dumps({"event_id": event_id, "resource_version": 1}, sort_keys=True).encode()
    headers = signing_headers(secret, "whk_synthetic", event_id, body, timestamp=1000, attempt_id="synthetic")
    assert verify_webhook(body, headers, {"whk_synthetic": secret}, now=1000)["resource_version"] == 1
    with pytest.raises(ValueError):
        verify_webhook(body+b" ", headers, {"whk_synthetic": secret}, now=1000)
    with pytest.raises(ValueError):
        verify_webhook(body, headers, {"whk_synthetic": secret}, now=1301)
