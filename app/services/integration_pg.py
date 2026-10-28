"""Identity-bound authorization observations from the existing single panel client."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.integration_authority import IntegrationAuthority, IntegrationPGEvidence, PG_EVIDENCE_MAX_AGE
from app.services.integration_policy import IntegrationError
from app.services.pasarguard import PasarGuardClient, PasarGuardError, get_pg, get_pg_for_principal
from app.services.pg_access import _resource_allows, acl_from_admin_payload, enrich_staff_pg_from_role
from app.services.pg_read_resilience import web_read_budget
from app.services.principal_pg_authz import apply_principal_pg_local_safety

MAX_EVIDENCE = 256
_EVIDENCE: dict[tuple[int, str, str], IntegrationPGEvidence] = {}


async def integration_pg_client(session: AsyncSession, authority: IntegrationAuthority) -> PasarGuardClient:
    if authority.owner.context.depth == 0:
        return get_pg()
    return await get_pg_for_principal(session, principal_id=authority.owner.principal_id, authenticate=False)


def principal_pg_staff(authority: IntegrationAuthority, admin: dict) -> dict:
    context = authority.owner.context
    features, role, _owner = acl_from_admin_payload(admin)
    if role is None:
        raise IntegrationError("authorization_unavailable")
    staff = {
        "role": "principal", "org_principal_id": context.principal_id, "org_depth": context.depth,
        "org_parent_id": context.parent_id, "org_status": context.principal_status,
        "org_visible_principal_ids": [context.principal_id], "pg_admin_username": context.pg_admin_username,
        "pg_credentials_ready": True, "pg_capabilities_ok": True, "pg_is_owner": _owner,
    }
    enriched = enrich_staff_pg_from_role(staff, features, role)
    return apply_principal_pg_local_safety(enriched) if context.depth in {1, 2} else enriched


def _catalog_actions(staff: dict, admin: dict) -> frozenset[str]:
    role = staff.get("pg_role")
    if not isinstance(role, dict):
        role = admin.get("role")
    if not isinstance(role, dict) or staff.get("pg_capabilities_ok") is False:
        return frozenset()
    if role.get("is_owner") and staff.get("org_depth") == 0:
        return frozenset({"templates", "groups"})
    permissions = role.get("permissions")
    if not isinstance(permissions, dict):
        return frozenset()
    return frozenset(resource for resource in ("templates", "groups") if _resource_allows(permissions, resource, "read", "read_simple"))


async def fetch_pg_evidence(authority: IntegrationAuthority, sessions: async_sessionmaker[AsyncSession],
                            *, allow_stale: bool = True) -> IntegrationPGEvidence:
    try:
        async with sessions() as session:
            client = await integration_pg_client(session, authority)
    except (httpx.HTTPError, PasarGuardError):
        raise IntegrationError("authorization_unavailable") from None
    expected = authority.owner.context.pg_admin_username
    if not expected:
        raise IntegrationError("authorization_unavailable")
    key = (authority.owner.principal_id, expected, client._read_cache_ident())
    try:
        with web_read_budget(seconds=3):
            admin = await client.request("GET", "/api/admin", use_cache=False)
        if not isinstance(admin, dict) or admin.get("username") != expected:
            _EVIDENCE.pop(key, None)
            raise IntegrationError("authorization_unavailable")
        staff = principal_pg_staff(authority, admin)
        if staff.get("pg_capabilities_ok") is False:
            _EVIDENCE.pop(key, None)
            raise IntegrationError("permission_denied")
        evidence = IntegrationPGEvidence(authority.owner.principal_id, expected, datetime.now(timezone.utc), True,
            frozenset(action for action, allowed in staff.get("pg_user_actions", {}).items() if allowed is True),
            _catalog_actions(staff, admin))
        if len(_EVIDENCE) >= MAX_EVIDENCE and key not in _EVIDENCE:
            _EVIDENCE.pop(min(_EVIDENCE, key=lambda item: _EVIDENCE[item].observed_at))
        _EVIDENCE[key] = evidence
        return evidence
    except (httpx.HTTPError, PasarGuardError) as exc:
        if isinstance(exc, PasarGuardError) and exc.status_code in {401, 403, 404, 405}:
            _EVIDENCE.pop(key, None)
            raise IntegrationError("permission_denied") from None
        cached = _EVIDENCE.get(key)
        if allow_stale and cached and datetime.now(timezone.utc) - cached.observed_at <= PG_EVIDENCE_MAX_AGE:
            return replace(cached, is_live=False)
        raise IntegrationError("authorization_unavailable") from None
