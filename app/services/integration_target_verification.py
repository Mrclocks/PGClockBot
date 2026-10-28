"""Verify exact saved technical targets and configured private delivery origins."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import unquote, urlsplit

from app.services.integration_observations import PanelIdentity, ServiceObservation, observe_service, strict_integer
from app.services.integration_operation_targets import PanelTarget
from app.services.integration_policy import IntegrationError

TOKEN = re.compile(r"[A-Za-z0-9_-]{16,512}", re.ASCII)


def panel_groups(payload: dict) -> tuple[int, ...]:
    groups = payload.get("group_ids", payload.get("groups"))
    if not isinstance(groups, list) or not groups or len(groups) > 100:
        raise IntegrationError("unsupported_terms")
    values = [strict_integer(item.get("id") if isinstance(item, dict) else item, minimum=1, maximum=2**31-1) for item in groups]
    if len(set(values)) != len(values):
        raise IntegrationError("unsupported_terms")
    return tuple(sorted(values))


def verify_target(payload: object, target: PanelTarget) -> ServiceObservation:
    observed = observe_service(payload, PanelIdentity(target.username, target.owner, target.panel_id))
    terms = target.payload
    if not isinstance(payload, dict) or payload.get("note") != target.marker or observed.status != terms["status"]:
        raise IntegrationError("service_identity_mismatch")
    if observed.limit_bytes != (terms.get("data_limit") or None) or panel_groups(payload) != tuple(sorted(terms["group_ids"])):
        raise IntegrationError("service_identity_mismatch")
    if "hwid_limit" in terms and (type(payload.get("hwid_limit")) not in {int, type(None)} or (payload.get("hwid_limit") or None) != (terms["hwid_limit"] or None)):
        raise IntegrationError("service_identity_mismatch")
    expire = terms.get("expire")
    if isinstance(expire, str):
        expire = int(datetime.fromisoformat(expire.replace("Z", "+00:00")).timestamp())
    observed_expire = int(observed.expires_at.timestamp()) if observed.expires_at else None
    if observed_expire != (expire or None) or observed.on_hold_seconds != terms.get("on_hold_expire_duration"):
        raise IntegrationError("service_identity_mismatch")
    return observed


def validated_delivery_url(payload: dict, *, panel_url: str, subscription_path: str) -> str:
    raw = payload.get("subscription_url")
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2048 or any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise IntegrationError("access_not_ready")
    try:
        url, configured = urlsplit(raw), urlsplit(panel_url)
        if (url.scheme != "https" or configured.scheme != "https" or not url.hostname or
                url.hostname.lower() != (configured.hostname or "").lower() or url.port != configured.port or
                url.username or url.password or url.query or url.fragment):
            raise ValueError
        prefix = subscription_path.rstrip("/")+"/"
        if not url.path.startswith(prefix) or not TOKEN.fullmatch(url.path[len(prefix):]) or unquote(url.path) != url.path:
            raise ValueError
    except ValueError:
        raise IntegrationError("access_not_ready") from None
    return raw
