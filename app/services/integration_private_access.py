"""Customer-qualified private URL and matching local QR access."""

from __future__ import annotations

import asyncio
import hashlib
import io
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import qrcode
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.integration_delivery_models import IntegrationDelivery
from app.db.integration_operation_models import ServiceMutationGuard
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.models import UserService
from app.services.integration_credentials import verify_credential
from app.services.integration_events import service_event
from app.services.integration_observations import PanelIdentity, observe_service
from app.services.integration_pg import integration_pg_client
from app.services.integration_policy import IntegrationError
from app.services.integration_service_reads import ReadAccess, mapped_customer, read_authority, scoped_services
from app.services.integration_target_verification import validated_delivery_url
from app.services.secret_box import decrypt_secret


@dataclass(frozen=True, slots=True)
class PrivateAccess:
    service_id: str
    customer_id: str
    subscription_url: str
    validated_at: datetime
    access_version: int


async def _private_row(session: AsyncSession, access: ReadAccess, customer_id: str, service_id: str) -> tuple:
    authority = await read_authority(session, access, frozenset({"services:read", "services:access"}))
    customer = await mapped_customer(session, authority, customer_id)
    row = (await session.execute(scoped_services(authority, customer.id).where(UserService.public_id == service_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    service, snapshot, _plan = row
    delivery = await session.get(IntegrationDelivery, service.id)
    if (snapshot is None or snapshot.state != "ready" or delivery is None or not service.pg_user_id or
            await session.get(ServiceMutationGuard, service.id) is not None or service.is_cancelled or service.cancellation_pending):
        raise IntegrationError("access_not_ready")
    return authority, service, snapshot, delivery


async def private_service_access(session: AsyncSession, access: ReadAccess, customer_id: str, service_id: str) -> PrivateAccess:
    authority, service, snapshot, delivery = await _private_row(session, access, customer_id, service_id)
    identity = PanelIdentity(service.pg_username, authority.owner.context.pg_admin_username or "", service.pg_user_id)
    digest, encrypted, version = delivery.url_digest, delivery.url_enc, delivery.access_version
    client = await integration_pg_client(session, authority)
    await session.rollback()
    async with asyncio.timeout(3):
        payload = await client.request("GET", f"/api/user/by-id/{identity.panel_id}", use_cache=False)
    try:
        observe_service(payload, identity)
        settings = get_settings()
        url = validated_delivery_url(payload, panel_url=settings.pg_base_url, subscription_path=settings.pg_subscription_path)
    except IntegrationError:
        await _review_access(session, access, customer_id, service_id)
        raise IntegrationError("access_not_ready") from None
    if not secrets.compare_digest(hashlib.sha256(url.encode()).hexdigest(), digest):
        await _review_access(session, access, customer_id, service_id)
        raise IntegrationError("access_not_ready")
    stored = await asyncio.to_thread(decrypt_secret, encrypted)
    if stored is None or not secrets.compare_digest(stored, url):
        await _review_access(session, access, customer_id, service_id)
        raise IntegrationError("access_not_ready")
    await verify_credential(session, access.bearer, lock_integration=True)
    _authority, _service, _snapshot, current = await _private_row(session, access, customer_id, service_id)
    if current.access_version != version or not secrets.compare_digest(current.url_digest, digest):
        raise IntegrationError("access_not_ready")
    now = datetime.now(timezone.utc)
    current.validated_at = now
    await session.commit()
    return PrivateAccess(service_id, customer_id, url, now, version)


async def _review_access(session: AsyncSession, access: ReadAccess, customer_id: str, service_id: str) -> None:
    await verify_credential(session, access.bearer, lock_integration=True)
    _authority, service, snapshot, _delivery = await _private_row(session, access, customer_id, service_id)
    result = await session.execute(update(IntegrationServiceSnapshot).where(IntegrationServiceSnapshot.service_id == service.id,
        IntegrationServiceSnapshot.resource_version == snapshot.resource_version,
        IntegrationServiceSnapshot.state == "ready").values(state="review", resource_version=IntegrationServiceSnapshot.resource_version+1))
    if result.rowcount:
        await service_event(session, service.id)
    await session.commit()


def render_access_qr(url: str) -> bytes:
    output = io.BytesIO()
    qrcode.make(url).save(output, format="PNG")
    return output.getvalue()
