"""Panel authorization uses the exact principal identity and bounded verified outage evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.integration_authority import require_pg_evidence, resolve_integration_authority
from app.services.integration_pg import _EVIDENCE, fetch_pg_evidence
from app.services.integration_policy import IntegrationError
from app.services.pasarguard import PasarGuardError
from test_integration_customers import database


class FakeClient:
    def __init__(self, ident="synthetic-client"):
        self.ident = ident
        self.request = AsyncMock(return_value={"username": "pg-race", "role": {"id": 1, "permissions": {
            "users": {"read": {"scope": 1}}, "templates": {"read": True}, "groups": {"read_simple": True},
        }}})

    def _read_cache_ident(self):
        return self.ident


def test_live_exact_identity_and_mapped_read_permissions() -> None:
    async def scenario():
        _EVIDENCE.clear()
        async with database() as (sessions, integration, key), sessions() as session:
            authority = await resolve_integration_authority(session, key.bearer)
            client = FakeClient()
            with patch("app.services.integration_pg.integration_pg_client", AsyncMock(return_value=client)):
                evidence = await fetch_pg_evidence(authority, sessions)
            assert evidence.is_live and evidence.principal_id == 11
            require_pg_evidence(authority, frozenset({"services:read", "plans:read"}), evidence)
            client.request.assert_awaited_once_with("GET", "/api/admin", use_cache=False)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["mismatch", "missing_role", "owner_equivalent", "denied", "expired", "new_client", "dispatch"])
def test_outage_cannot_borrow_identity_or_override_live_denial(failure):
    async def scenario():
        _EVIDENCE.clear()
        async with database() as (sessions, integration, key), sessions() as session:
            authority = await resolve_integration_authority(session, key.bearer)
            client = FakeClient()
            with patch("app.services.integration_pg.integration_pg_client", AsyncMock(return_value=client)):
                await fetch_pg_evidence(authority, sessions)
                client.request.side_effect = httpx.ConnectError("synthetic outage")
                cached = await fetch_pg_evidence(authority, sessions)
                assert not cached.is_live
                if failure == "mismatch":
                    client.request.side_effect = None
                    client.request.return_value = {"username": "other-principal", "role": {"is_owner": True}}
                elif failure == "missing_role":
                    client.request.side_effect = None
                    client.request.return_value = {"username": "pg-race"}
                elif failure == "owner_equivalent":
                    client.request.side_effect = None
                    client.request.return_value = {"username": "pg-race", "role": {"is_owner": True}}
                elif failure == "denied":
                    client.request.side_effect = PasarGuardError("synthetic denial", 403)
                elif failure == "expired":
                    for cache_key, value in list(_EVIDENCE.items()):
                        _EVIDENCE[cache_key] = replace(value, observed_at=datetime.now(timezone.utc)-timedelta(minutes=6))
                elif failure == "new_client":
                    client.ident = "replacement-credentials"
                with pytest.raises(IntegrationError):
                    await fetch_pg_evidence(authority, sessions, allow_stale=failure != "dispatch")
    asyncio.run(scenario())


def test_api_client_construction_does_not_authenticate_with_an_open_database_transaction():
    from app.services.integration_pg import integration_pg_client
    from app.services.pasarguard import PasarGuardClient, reset_pg

    async def scenario():
        reset_pg()
        async with database() as (sessions, integration, key), sessions() as session:
            authority = await resolve_integration_authority(session, key.bearer)
            with patch("app.services.secret_box.decrypt_secret", return_value="synthetic-api-key"), patch.object(
                PasarGuardClient, "ensure_token", AsyncMock()) as authenticate:
                client = await integration_pg_client(session, authority)
                authenticate.assert_not_called()
                assert isinstance(client, PasarGuardClient)
                await client._client.aclose()
        reset_pg()
    asyncio.run(scenario())
