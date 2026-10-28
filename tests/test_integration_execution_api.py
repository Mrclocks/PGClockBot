"""Backend submissions are durable, scoped and disabled by their execution control."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

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
