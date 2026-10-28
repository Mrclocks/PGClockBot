"""Versioned destinations, isolated signing keys and durable bounded event delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uuid import uuid4

from app.db.integration_models import Integration
from app.db.integration_webhook_models import IntegrationEvent, IntegrationEventAttempt, IntegrationWebhookDestination, IntegrationWebhookKey
from app.services.authz import can_shop
from app.services.integration_policy import IntegrationError, utc_time
from app.services.integration_webhook_transport import WebhookResponse, WebhookTarget, resolve_webhook, send_pinned_webhook, webhook_target
from app.services.integrations import managed_integration
from app.services.secret_box import decrypt_secret, encrypt_secret

RETRY_OFFSETS = (0, 60, 300, 1800, 7200, 21600, 86400, 172800, 259200, 345600, 432000, 518400, 604800)
Resolver = Callable[[WebhookTarget], Awaitable[tuple[str, ...]]]
Sender = Callable[[WebhookTarget, tuple[str, ...], bytes, dict[str, str]], Awaitable[WebhookResponse]]


@dataclass(frozen=True, slots=True)
class WebhookIssuance:
    key_id: str
    secret: str = field(repr=False)


async def configure_destination(session: AsyncSession, staff: dict, integration_id: str, url: str, *, enabled: bool,
                                 resolver: Resolver = resolve_webhook) -> None:
    integration, owner = await managed_integration(session, staff, integration_id)
    if not can_shop(owner.context, "integration_webhooks_manage") or type(enabled) is not bool:
        raise IntegrationError("permission_denied")
    target = webhook_target(url)
    await session.rollback()
    await resolver(target)
    integration, owner = await managed_integration(session, staff, integration_id, lock=True)
    if not can_shop(owner.context, "integration_webhooks_manage"):
        raise IntegrationError("permission_denied")
    row = await session.get(IntegrationWebhookDestination, integration.id)
    if row is None:
        session.add(IntegrationWebhookDestination(integration_id=integration.id, url=target.url, enabled=enabled))
    else:
        row.url, row.enabled = target.url, enabled
        row.version += 1
        row.updated_at = datetime.now(timezone.utc)


async def rotate_webhook_key(session: AsyncSession, staff: dict, integration_id: str, *, overlap_minutes: int = 0) -> WebhookIssuance:
    if type(overlap_minutes) is not int or not 0 <= overlap_minutes <= 1440:
        raise IntegrationError("invalid_request")
    integration, owner = await managed_integration(session, staff, integration_id, lock=True)
    if not can_shop(owner.context, "integration_webhooks_manage"):
        raise IntegrationError("permission_denied")
    secret, now = secrets.token_hex(32), datetime.now(timezone.utc)
    encrypted = await asyncio.to_thread(encrypt_secret, secret)
    if not encrypted:
        raise IntegrationError("security_unavailable")
    deadline = now+timedelta(minutes=overlap_minutes)
    await session.execute(update(IntegrationWebhookKey).where(IntegrationWebhookKey.integration_id == integration.id,
        or_(IntegrationWebhookKey.expires_at.is_(None), IntegrationWebhookKey.expires_at > deadline)).values(expires_at=deadline))
    row = IntegrationWebhookKey(integration_id=integration.id, secret_enc=encrypted)
    session.add(row)
    await session.flush()
    return WebhookIssuance(row.id, secret)


def signing_headers(secret: str, key_id: str, event_id: str, body: bytes, *, timestamp: int, attempt_id: str) -> dict[str, str]:
    message = str(timestamp).encode()+b"."+event_id.encode()+b"."+body
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return {"X-PGClockBot-Event-ID": event_id, "X-PGClockBot-Timestamp": str(timestamp), "X-PGClockBot-Key-ID": key_id,
        "X-PGClockBot-Signature": "v1="+signature, "X-PGClockBot-Attempt-ID": attempt_id}


@dataclass(frozen=True, slots=True)
class EventLease:
    internal_id: int
    integration_id: str
    event_id: str
    body: bytes
    url: str
    token: str
    attempt_id: str
    attempt_number: int
    created_at: datetime
    key_id: str | None
    key_enc: str | None = field(repr=False)


async def _exhaust_stale_attempt(session: AsyncSession, row: IntegrationEvent, now: datetime) -> bool:
    if row.attempts < len(RETRY_OFFSETS):
        return False
    await session.execute(update(IntegrationEvent).where(IntegrationEvent.id == row.id, IntegrationEvent.attempts >= len(RETRY_OFFSETS),
        or_(IntegrationEvent.state == "pending", (IntegrationEvent.state == "delivering") & (IntegrationEvent.lease_expires_at <= now)),
    ).values(state="exhausted", lease_token=None, lease_expires_at=None, last_failure="delivery_unknown").execution_options(synchronize_session=False))
    await session.commit()
    return True


async def claim_event(session: AsyncSession, event_id: int, *, now: datetime) -> EventLease | None:
    integration_id = await session.scalar(update(Integration).where(Integration.id.in_(select(IntegrationEvent.integration_id).where(
        IntegrationEvent.id == event_id)), Integration.status == "active").values(revision=Integration.revision+1).returning(Integration.id))
    if integration_id is None:
        await session.rollback()
        return None
    active = await session.scalar(select(func.count()).select_from(IntegrationEvent).where(IntegrationEvent.integration_id == integration_id,
        IntegrationEvent.state == "delivering", IntegrationEvent.lease_expires_at > now))
    if active >= 2:
        await session.rollback()
        return None
    row = await session.get(IntegrationEvent, event_id, populate_existing=True)
    destination = await session.get(IntegrationWebhookDestination, integration_id)
    if destination is None or not destination.enabled or row.destination_version != destination.version:
        await session.rollback()
        return None
    if await _exhaust_stale_attempt(session, row, now):
        return None
    token = secrets.token_hex(32)
    claimed = await session.execute(update(IntegrationEvent).where(IntegrationEvent.id == event_id,
        IntegrationEvent.integration_id == integration_id, IntegrationEvent.attempts < len(RETRY_OFFSETS),
        IntegrationEvent.next_attempt_at <= now, or_(IntegrationEvent.state == "pending",
            (IntegrationEvent.state == "delivering") & (IntegrationEvent.lease_expires_at <= now)),
    ).values(state="delivering", lease_token=token, lease_expires_at=now+timedelta(seconds=30), attempts=IntegrationEvent.attempts+1)
        .returning(IntegrationEvent.attempts).execution_options(synchronize_session=False))
    attempt = claimed.scalar_one_or_none()
    if attempt is None:
        await session.rollback()
        return None
    key = await session.scalar(select(IntegrationWebhookKey).where(IntegrationWebhookKey.integration_id == integration_id,
        IntegrationWebhookKey.expires_at.is_(None)).order_by(IntegrationWebhookKey.created_at.desc(), IntegrationWebhookKey.id.desc()).limit(1))
    attempt_id = uuid4().hex
    session.add(IntegrationEventAttempt(id=attempt_id, event_id=event_id, key_id=key.id if key else None, created_at=now))
    lease = EventLease(event_id, integration_id, row.event_id, row.body.encode(), destination.url, token, attempt_id, attempt,
        utc_time(row.created_at), key.id if key else None, key.secret_enc if key else None)
    await session.commit()
    return lease


async def _finish_event(session: AsyncSession, lease: EventLease, response: WebhookResponse | None, failure: str | None,
                        *, now: datetime, latency_ms: int) -> None:
    acknowledged = response is not None and 200 <= response.status < 300
    state = "acknowledged" if acknowledged else "exhausted" if lease.attempt_number >= len(RETRY_OFFSETS) else "pending"
    offset = RETRY_OFFSETS[min(lease.attempt_number, len(RETRY_OFFSETS)-1)]
    jitter = secrets.randbelow(31)
    next_attempt = max(lease.created_at+timedelta(seconds=offset+jitter), now+timedelta(seconds=max(1, response.retry_after if response else 0)))
    await session.execute(update(IntegrationEvent).where(IntegrationEvent.id == lease.internal_id,
        IntegrationEvent.integration_id == lease.integration_id, IntegrationEvent.state == "delivering",
        IntegrationEvent.lease_token == lease.token, IntegrationEvent.lease_expires_at > now,
    ).values(state=state, next_attempt_at=next_attempt, lease_token=None, lease_expires_at=None,
        acknowledged_at=now if acknowledged else None, last_status=response.status if response else None,
        last_failure=None if acknowledged else failure or "http_status").execution_options(synchronize_session=False))
    await session.execute(update(IntegrationEventAttempt).where(IntegrationEventAttempt.id == lease.attempt_id,
        IntegrationEventAttempt.event_id == lease.internal_id).values(status=response.status if response else None,
        failure=failure, latency_ms=min(latency_ms, 2**31-1)))
    await session.commit()


async def deliver_event(sessions: async_sessionmaker[AsyncSession], event_id: int, *, resolver: Resolver = resolve_webhook,
                        sender: Sender = send_pinned_webhook, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    async with sessions() as session:
        lease = await claim_event(session, event_id, now=now)
    if lease is None:
        return False
    response, failure, cancelled = None, None, None
    started = time.monotonic()
    try:
        async with asyncio.timeout(5):
            if not lease.key_enc or not lease.key_id:
                raise IntegrationError("security_unavailable")
            secret = await asyncio.to_thread(decrypt_secret, lease.key_enc)
            if secret is None:
                raise IntegrationError("security_unavailable")
            target = webhook_target(lease.url)
            addresses = await resolver(target)
            headers = signing_headers(secret, lease.key_id, lease.event_id, lease.body,
                timestamp=int(now.timestamp()), attempt_id=lease.attempt_id)
            response = await sender(target, addresses, lease.body, headers)
    except BaseException as error:
        failure = "destination_denied" if isinstance(error, IntegrationError) else "timeout" if isinstance(error, TimeoutError) else "transport"
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            cancelled = error
    async with sessions() as session:
        await _finish_event(session, lease, response, failure, now=now+timedelta(seconds=time.monotonic()-started),
            latency_ms=int((time.monotonic()-started)*1000))
    if cancelled:
        raise cancelled
    return response is not None and 200 <= response.status < 300
