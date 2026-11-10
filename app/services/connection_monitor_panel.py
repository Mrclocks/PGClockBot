"""Fresh authenticated catalog and dedicated-account evidence from the single panel."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget
from app.py_compat import timeout as async_timeout
from app.services.connection_monitor_accounts import MonitorAccountError, panel_account_error, validate_monitor_account
from app.services.connection_monitor_settings import reveal_monitor_token
from app.services.connection_status_types import ConnectionStatus, MonitorReason
from app.services.pasarguard import PasarGuardError, get_pg


@dataclass(frozen=True, slots=True)
class MonitorAccount:
    pg_user_id: int
    revision: str
    encrypted_token: str = field(repr=False)
    username: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MonitorCatalog:
    native_configs: list[dict[str, Any]] = field(repr=False)
    checked_at: datetime


async def read_monitor_catalog(account: MonitorAccount) -> MonitorCatalog:
    pg = get_pg()
    token = await reveal_monitor_token(account.encrypted_token)
    try:
        async with async_timeout(10):
            try:
                user = await pg.get_user_by_id(account.pg_user_id)
            except PasarGuardError as exc:
                # Deleted panel user: stop polling rather than retrying every cycle.
                if exc.status_code == 404:
                    raise MonitorAccountError("user_missing") from None
                raise
            validated = validate_monitor_account(user, username=account.username)
            revision = hashlib.sha256(f"{validated.user_id}:{token}".encode()).hexdigest()
            if validated.user_id != account.pg_user_id or not secrets.compare_digest(revision, account.revision):
                raise MonitorAccountError("account_changed")
            # Panel URLs contain an issuance timestamp; a new alias does not revoke this token.
            try:
                native = await pg.subscription_xray(token, subscription_url=user["subscription_url"])
            except PasarGuardError as exc:
                if exc.status_code in (401, 404):
                    raise MonitorAccountError("account_changed") from None
                raise
    except MonitorAccountError:
        raise
    except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
        raise panel_account_error(exc) from None
    return MonitorCatalog(native, datetime.now(timezone.utc))


async def mark_monitor_user_missing(
    session: AsyncSession,
    *,
    state_id: int,
    now: datetime | None = None,
) -> bool:
    """Disable platform monitoring when the dedicated panel user no longer exists.

    Idempotent and scoped to ``reseller_id IS NULL`` so another shop's rows stay untouched.
    The owner can re-enable after restoring the user.
    """
    if type(state_id) is not int or state_id <= 0:
        return False
    moment = now or datetime.now(timezone.utc)
    await session.execute(
        update(ConnectionMonitorState).where(
            ConnectionMonitorState.id == int(state_id),
            ConnectionMonitorState.reseller_id.is_(None),
        ).values(
            enabled=False,
            cycle_token=None,
            cycle_lease_until=None,
            catalog_synced_at=None,
        )
    )
    await session.execute(
        update(ConnectionMonitorTarget).where(
            ConnectionMonitorTarget.reseller_id.is_(None),
        ).values(
            test_enabled=False,
            is_public=False,
            claim_token=None,
            claim_until=None,
            manual_pending=False,
        )
    )
    await session.execute(
        update(ConnectionMonitorSnapshot).where(
            ConnectionMonitorSnapshot.reseller_id.is_(None),
        ).values(
            status=ConnectionStatus.PAUSED.value,
            reason=MonitorReason.USER_MISSING.value,
            checked_at=moment,
            delay_ms=None,
            success_streak=0,
            failure_streak=0,
            recovery_required=ConnectionMonitorSnapshot.has_observation,
            samples_json="[]",
        )
    )
    await session.commit()
    return True
