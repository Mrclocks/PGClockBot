"""Strict panel observations shared by reads, execution and conservative reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.formatting import hold_duration_from_info, parse_expire
from app.services.integration_policy import IntegrationError, utc_time
from app.services.pg_user_scope import pg_user_owner_username

MAX_PUBLIC_BYTES = 2**53 - 1
MAX_DURATION_SECONDS = 315360000
PANEL_STATES = frozenset({"active", "on_hold", "disabled", "limited", "expired"})


@dataclass(frozen=True, slots=True)
class PanelIdentity:
    username: str
    owner: str
    panel_id: int | None = None


@dataclass(frozen=True, slots=True)
class ServiceObservation:
    panel_id: int
    status: str
    limit_bytes: int | None
    used_bytes: int
    expires_at: datetime | None
    on_hold_seconds: int | None
    volume_unlimited: bool
    time_unlimited: bool


def strict_integer(value: object, *, minimum: int = 0, maximum: int = MAX_PUBLIC_BYTES) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise IntegrationError("unsupported_terms")
    return value


def observe_service(payload: object, expected: PanelIdentity) -> ServiceObservation:
    if not isinstance(payload, dict) or not expected.owner:
        raise IntegrationError("unsupported_terms")
    panel_id = strict_integer(payload.get("id"), minimum=1, maximum=2**31-1)
    if (payload.get("username") != expected.username or pg_user_owner_username(payload) != expected.owner.lower()
            or expected.panel_id is not None and panel_id != expected.panel_id):
        raise IntegrationError("service_identity_mismatch")
    status = payload.get("status")
    if not isinstance(status, str) or status not in PANEL_STATES or "data_limit" not in payload or "expire" not in payload:
        raise IntegrationError("unsupported_terms")
    used = strict_integer(payload.get("used_traffic"))
    limit = payload["data_limit"]
    if limit is not None:
        limit = strict_integer(limit)
    raw_expire = payload["expire"]
    if isinstance(raw_expire, (bool, float)):
        raise IntegrationError("unsupported_terms")
    expires = parse_expire(raw_expire)
    if raw_expire not in (None, 0) and expires is None:
        raise IntegrationError("unsupported_terms")
    hold = None
    if status == "on_hold" and expires is None:
        raw_hold = payload.get("on_hold_expire_duration", payload.get("expire_duration"))
        hold = strict_integer(raw_hold, minimum=1, maximum=MAX_DURATION_SECONDS)
        if hold_duration_from_info(payload) != hold:
            raise IntegrationError("unsupported_terms")
    return ServiceObservation(panel_id, status, limit or None, used, utc_time(expires) if expires else None,
                              hold, not limit, expires is None and hold is None)
