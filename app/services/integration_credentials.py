"""Issue one-time bearer keys and enforce bounded, revocable credential lifetimes."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_models import Integration, IntegrationCredential
from app.services.integration_transactions import finish_integration_write
from app.services.integration_policy import IntegrationError, stored_scopes, utc_time, validate_scopes
from app.services.integrations import managed_integration, validate_owner_grants

DEFAULT_CREDENTIAL_LIFETIME = timedelta(days=90)
MAX_CREDENTIAL_LIFETIME = timedelta(days=365)
MAX_ROTATION_OVERLAP = timedelta(hours=24)
_TOKEN_PATTERN = re.compile(r"pgint_([0-9a-f]{32})\.([0-9a-f]{64})", re.ASCII)
_DUMMY_DIGEST = hashlib.sha256(b"PGClockBot unknown integration credential").hexdigest()


@dataclass(frozen=True, slots=True)
class CredentialView:
    selector: str
    integration_id: str
    scopes: frozenset[str]
    status: str
    expires_at: datetime
    rotated_at: datetime | None


@dataclass(frozen=True, slots=True)
class CredentialIssuance:
    credential: CredentialView
    bearer: str = field(repr=False)


def credential_view(row: IntegrationCredential) -> CredentialView:
    return CredentialView(
        row.selector, row.integration_id, stored_scopes(row.scopes), row.status,
        utc_time(row.expires_at), utc_time(row.rotated_at) if row.rotated_at else None,
    )


def _expiry(expires_at: datetime | None, now: datetime) -> datetime:
    if expires_at is None:
        return now + DEFAULT_CREDENTIAL_LIFETIME
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise IntegrationError("invalid_request")
    expires_at = expires_at.astimezone(timezone.utc)
    if not now < expires_at <= now + MAX_CREDENTIAL_LIFETIME:
        raise IntegrationError("invalid_request")
    return expires_at


def _new_credential(
    integration: Integration, *, scopes: frozenset[str], expires_at: datetime, now: datetime,
) -> tuple[IntegrationCredential, str]:
    secret = secrets.token_hex(32)
    selector = secrets.token_hex(16)
    row = IntegrationCredential(
        selector=selector, integration_id=integration.id, scopes=",".join(sorted(scopes)),
        secret_digest=hashlib.sha256(secret.encode("ascii")).hexdigest(),
        created_at=now, expires_at=expires_at, status="active",
    )
    return row, f"pgint_{selector}.{secret}"


async def issue_credential(
    session: AsyncSession, authenticated_staff: dict, integration_id: str, *,
    scopes: frozenset[str] = frozenset(), expires_at: datetime | None = None, commit: bool = True,
) -> CredentialIssuance:
    scopes = validate_scopes(scopes)
    now = datetime.now(timezone.utc)
    expiry = _expiry(expires_at, now)
    integration, owner = await managed_integration(session, authenticated_staff, integration_id, lock=True)
    validate_owner_grants(owner, scopes)
    if integration.status == "retired" or not scopes <= stored_scopes(integration.grants):
        raise IntegrationError("permission_denied")
    row, bearer = _new_credential(integration, scopes=scopes, expires_at=expiry, now=now)
    session.add(row)
    await finish_integration_write(session, commit=commit)
    return CredentialIssuance(credential_view(row), bearer)


async def read_credential(
    session: AsyncSession, authenticated_staff: dict, integration_id: str, selector: str,
) -> CredentialView:
    await managed_integration(session, authenticated_staff, integration_id)
    row = await session.scalar(select(IntegrationCredential).where(
        IntegrationCredential.selector == selector, IntegrationCredential.integration_id == integration_id,
    ).execution_options(populate_existing=True))
    if row is None:
        raise IntegrationError("not_found")
    return credential_view(row)


async def rotate_credential(
    session: AsyncSession, authenticated_staff: dict, integration_id: str, selector: str, *,
    overlap: timedelta = timedelta(0), expires_at: datetime | None = None, commit: bool = True,
) -> CredentialIssuance:
    if not isinstance(overlap, timedelta) or not timedelta(0) <= overlap <= MAX_ROTATION_OVERLAP:
        raise IntegrationError("invalid_request")
    now = datetime.now(timezone.utc)
    expiry = _expiry(expires_at, now)
    integration, owner = await managed_integration(session, authenticated_staff, integration_id, lock=True)
    old = await session.scalar(select(IntegrationCredential).where(
        IntegrationCredential.selector == selector, IntegrationCredential.integration_id == integration.id,
    ).execution_options(populate_existing=True))
    if old is None:
        raise IntegrationError("not_found")
    scopes = stored_scopes(old.scopes)
    validate_owner_grants(owner, scopes)
    if integration.status == "retired" or not scopes <= stored_scopes(integration.grants):
        raise IntegrationError("permission_denied")
    claimed = await session.execute(update(IntegrationCredential).where(
        IntegrationCredential.selector == selector, IntegrationCredential.integration_id == integration.id,
        IntegrationCredential.status == "active", IntegrationCredential.expires_at > now,
        IntegrationCredential.rotated_at.is_(None),
    ).values(rotated_at=now, expires_at=min(utc_time(old.expires_at), now + overlap))
        .execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise IntegrationError("invalid_credential")
    row, bearer = _new_credential(integration, scopes=scopes, expires_at=expiry, now=now)
    session.add(row)
    await finish_integration_write(session, commit=commit)
    return CredentialIssuance(credential_view(row), bearer)


async def revoke_credential(
    session: AsyncSession, authenticated_staff: dict, integration_id: str, selector: str, *, commit: bool = True,
) -> None:
    await managed_integration(session, authenticated_staff, integration_id, lock=True)
    result = await session.execute(update(IntegrationCredential).where(
        IntegrationCredential.selector == selector, IntegrationCredential.integration_id == integration_id,
    ).values(status="revoked", revoked_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        raise IntegrationError("not_found")
    await finish_integration_write(session, commit=commit)


async def verify_credential(
    session: AsyncSession, bearer: str, *, lock_integration: bool = False,
) -> IntegrationCredential:
    match = _TOKEN_PATTERN.fullmatch(bearer) if isinstance(bearer, str) else None
    selector, secret = match.groups() if match else ("", "invalid")
    if lock_integration:
        # Acquire the write lock before reading to avoid SQLite WAL snapshot upgrades.
        # A failed verification must roll back this transaction, including the revision.
        await session.execute(update(Integration).where(Integration.id.in_(
            select(IntegrationCredential.integration_id).where(IntegrationCredential.selector == selector),
        )).values(revision=Integration.revision + 1))
    row = await session.scalar(select(IntegrationCredential).where(
        IntegrationCredential.selector == selector,
    ).execution_options(populate_existing=True))
    digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    valid = secrets.compare_digest(digest, row.secret_digest if row is not None else _DUMMY_DIGEST)
    if not valid or row is None or row.status != "active" or utc_time(row.expires_at) <= datetime.now(timezone.utc):
        raise IntegrationError("invalid_credential")
    return row
