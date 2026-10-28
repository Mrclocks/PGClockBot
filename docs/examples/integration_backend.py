"""Generic backend building blocks; importing this example makes no requests."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Mapping

import httpx


@dataclass(slots=True)
class IntegrationBackend:
    """Keep the client and credential on your server, with normal TLS verification."""
    client: httpx.AsyncClient
    credential: str = field(repr=False)

    async def submit_provision(self, *, customer_id: str, plan_id: str, terms_revision: str,
                               external_operation_ref: str, idempotency_key: str) -> dict:
        # Persist both caller-generated identifiers with your business intent before
        # this call. Reuse them after timeouts; do not create another paid intent.
        response = await self.client.post("/api/v1/provisioning-requests", timeout=10,
            headers={"Authorization": f"Bearer {self.credential}", "Idempotency-Key": idempotency_key},
            json={"customer_id": customer_id, "plan_id": plan_id, "terms_revision": terms_revision,
                "external_operation_ref": external_operation_ref})
        response.raise_for_status()
        return response.json()

    async def poll_operation(self, *, customer_id: str, operation_id: str) -> dict:
        # Unknown/review states require polling or operator investigation. A new
        # external reference would authorize a distinct technical intent.
        response = await self.client.get(f"/api/v1/operations/{operation_id}", params={"customer_id": customer_id},
            timeout=10, headers={"Authorization": f"Bearer {self.credential}"})
        response.raise_for_status()
        return response.json()


def verify_webhook(body: bytes, headers: Mapping[str, str], keys: Mapping[str, str], *, now: int | None = None) -> dict:
    """Verify exact raw bytes before parsing; caller still needs durable deduplication."""
    if len(body) > 16*1024:
        raise ValueError("Invalid webhook")
    normalized = {name.lower(): value for name, value in headers.items()}
    if len(normalized) != len(headers):
        raise ValueError("Ambiguous webhook headers")
    headers = normalized
    event_id = headers.get("x-pgclockbot-event-id", "")
    timestamp = headers.get("x-pgclockbot-timestamp", "")
    key = keys.get(headers.get("x-pgclockbot-key-id", ""))
    signature = headers.get("x-pgclockbot-signature", "")
    if (key is None or not re.fullmatch(r"evt_[0-9a-f]{32}", event_id) or not timestamp.isascii()
            or not timestamp.isdecimal() or len(timestamp) > 12):
        raise ValueError("Invalid webhook")
    if abs((int(time.time()) if now is None else now)-int(timestamp)) > 300:
        raise ValueError("Stale webhook")
    expected = "v1="+hmac.new(key.encode(), timestamp.encode()+b"."+event_id.encode()+b"."+body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid webhook")
    event = json.loads(body)
    if not isinstance(event, dict) or event.get("event_id") != event_id or type(event.get("resource_version")) is not int or event["resource_version"] <= 0:
        raise ValueError("Invalid webhook")
    # In ONE receiver transaction, insert event_id under a UNIQUE constraint and
    # apply only newer resource versions. Acknowledge only after that commit.
    # Poll current state for gaps. This example supplies no in-memory dedup store.
    return event
