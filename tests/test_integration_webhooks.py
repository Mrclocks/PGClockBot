"""Stable outbox delivery, destination binding and SSRF tests never use the network."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, update

from app.db.integration_models import Integration
from app.db.integration_webhook_models import IntegrationEvent, IntegrationWebhookDestination
from app.db.models import ResellerProfile
from app.services.integration_events import append_event
from app.services.integration_policy import IntegrationError
from app.services.integration_webhook_transport import WebhookResponse, validated_addresses, webhook_target
from app.services.integration_webhooks import RETRY_OFFSETS, configure_destination, deliver_event, rotate_webhook_key
from test_integration_concurrency import STAFF
from test_integration_service_reads import read_database


@pytest.mark.parametrize("url", ["http://hooks.example.com/x", "https://hooks.example.com:444/x", "https://a@hooks.example.com/x",
    "https://hooks.example.com/x?token=secret", "https://hooks.example.com/x#y", "https://localhost/x", "https://hooks.local/x",
    "https://hooks.example.com/%0d%0aheader", "https://127.0.0.1/x"])
def test_unsafe_destinations_are_rejected_before_dns(url):
    with pytest.raises(IntegrationError):
        webhook_target(url)


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["::1"], ["::ffff:127.0.0.1"],
    ["8.8.8.8", "192.168.1.2"], ["224.0.0.1"], [], ["fd00::1"]])
def test_all_dns_answers_must_be_public(addresses):
    with pytest.raises(IntegrationError):
        validated_addresses(addresses)


async def prepare(sessions, integration):
    async with sessions() as session:
        profile = await session.get(ResellerProfile, 11)
        profile.web_permissions += ",integration_webhooks_manage"
        await session.commit()
        resolver = AsyncMock(return_value=("8.8.8.8",))
        await configure_destination(session, STAFF, integration, "https://hooks.example.com/events", enabled=True, resolver=resolver)
        with patch("app.services.secret_box._root_secret", return_value=b"synthetic-webhook-encryption-root"):
            issuance = await rotate_webhook_key(session, STAFF, integration)
        await append_event(session, integration, event_type="operation.updated", resource_id="op_synthetic",
            customer_id="cus_synthetic", version=1, fields={"state": "needs_review", "failure_code": "outcome_unknown"})
        await session.commit()
        event = await session.scalar(select(IntegrationEvent))
        return event.id, issuance


def test_retry_has_stable_body_new_attempt_signature_and_at_least_once_identity():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access):
            event_id, issuance = await prepare(sessions, integration)
            deliveries = []
            async def send(target, addresses, body, headers):
                assert addresses == ("8.8.8.8",) and target.hostname == "hooks.example.com"
                expected = hmac.new(issuance.secret.encode(), headers["X-PGClockBot-Timestamp"].encode()+b"."+
                    headers["X-PGClockBot-Event-ID"].encode()+b"."+body, hashlib.sha256).hexdigest()
                assert hmac.compare_digest(headers["X-PGClockBot-Signature"], "v1="+expected)
                deliveries.append((body, headers))
                return WebhookResponse(503, 300) if len(deliveries) == 1 else WebhookResponse(204)
            resolver = AsyncMock(return_value=("8.8.8.8",))
            with patch("app.services.secret_box._root_secret", return_value=b"synthetic-webhook-encryption-root"):
                assert not await deliver_event(sessions, event_id, resolver=resolver, sender=send)
                async with sessions() as session:
                    event = await session.get(IntegrationEvent, event_id)
                    assert event.state == "pending" and event.attempts == 1
                    now = event.next_attempt_at.replace(tzinfo=timezone.utc)+timedelta(seconds=1)
                assert await deliver_event(sessions, event_id, resolver=resolver, sender=send, now=now)
                assert not await deliver_event(sessions, event_id, resolver=resolver, sender=send, now=now)
            assert len(deliveries) == 2 and deliveries[0][0] == deliveries[1][0]
            assert deliveries[0][1]["X-PGClockBot-Attempt-ID"] != deliveries[1][1]["X-PGClockBot-Attempt-ID"]
            assert set(json.loads(deliveries[0][0])) == {"event_id", "type", "occurred_at", "resource_id", "customer_id", "resource_version", "state", "failure_code"}
    asyncio.run(scenario())


def test_destination_changes_and_disabled_integrations_pause_historic_events():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access):
            event_id, issuance = await prepare(sessions, integration)
            resolver, sender = AsyncMock(return_value=("8.8.8.8",)), AsyncMock(return_value=WebhookResponse(200))
            async with sessions() as session:
                await session.execute(update(Integration).where(Integration.id == integration).values(status="disabled"))
                await session.commit()
            assert not await deliver_event(sessions, event_id, resolver=resolver, sender=sender)
            async with sessions() as session:
                await session.execute(update(Integration).where(Integration.id == integration).values(status="active"))
                destination = await session.get(IntegrationWebhookDestination, integration)
                destination.version += 1
                destination.url = "https://replacement.example.com/events"
                await session.commit()
            assert not await deliver_event(sessions, event_id, resolver=resolver, sender=sender)
            sender.assert_not_awaited()
            resolver.assert_not_awaited()
    asyncio.run(scenario())


def test_rebinding_is_rechecked_and_exhaustion_retains_original_event():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan, access):
            event_id, issuance = await prepare(sessions, integration)
            async def denied(target):
                return validated_addresses(["8.8.8.8", "127.0.0.1"])
            sender = AsyncMock(return_value=WebhookResponse(200))
            with patch("app.services.secret_box._root_secret", return_value=b"synthetic-webhook-encryption-root"):
                for _ in range(13):
                    async with sessions() as session:
                        event = await session.get(IntegrationEvent, event_id)
                        now = event.next_attempt_at.replace(tzinfo=timezone.utc)+timedelta(seconds=1)
                    assert not await deliver_event(sessions, event_id, resolver=denied, sender=sender, now=now)
            sender.assert_not_awaited()
            async with sessions() as session:
                event = await session.get(IntegrationEvent, event_id)
                assert event.state == "exhausted" and event.attempts == len(RETRY_OFFSETS) == 13 and event.last_failure == "destination_denied"
                assert await session.scalar(select(func.count()).select_from(IntegrationEvent)) == 1
    asyncio.run(scenario())
